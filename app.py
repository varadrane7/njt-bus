"""NJ Transit bus ETA sidecar for Home Assistant.

Polls the NJT Bus GTFS-RT G2 API (trip updates), combines it with the static
GTFS schedule, and serves clean "minutes until next bus" JSON for a single stop.

Endpoints (GET, read-only, no secrets in output):
  /api/route/<route>   e.g. /api/route/158   -> flat JSON, one route
  /api/next            all configured routes
  /debug               what the service matched (headsigns seen at the stop, stats)
  /healthz             200 when the last poll succeeded recently
"""
import csv
import io
import json
import logging
import math
import os
import threading
import time
import zipfile
from datetime import date, datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2 as rt

log = logging.getLogger("njt")


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


BASE_URL = env("NJT_BASE_URL", "https://pcsdata.njtransit.com/api/GTFSG2").rstrip("/")
STOP = (env("STOP_ID") or "").strip()
POLL_SECONDS = int(env("POLL_SECONDS", "30"))
STALE_AFTER = int(env("STALE_AFTER_SECONDS", "180"))
SCHEDULE_HORIZON = int(env("SCHEDULE_HORIZON_MINUTES", "180")) * 60
STATIC_REFRESH_S = int(env("STATIC_REFRESH_HOURS", "24")) * 3600
TOKEN_MAX_AGE_S = 20 * 3600  # NJT tokens last ~24h; refresh early
DATA_DIR = env("DATA_DIR", "/data")
TZ = ZoneInfo(env("TZ", "America/New_York"))
GRACE = 90  # seconds an ETA may be in the past before we drop it
GPS_ENABLED = env("GPS_ESTIMATE", "1") != "0"
GPS_SPEEDUP_MS = float(env("GPS_SPEEDUP_MPH", "5")) * 0.44704  # assume bus is this much faster than timetable pace (optimistic on purpose)
GPS_MIN_SPEED_MS = 1.5                                     # floor for timetable-implied speed (avoids huge times on dwell-heavy stretches)
GPS_MAX_AGE = int(env("GPS_MAX_AGE_SECONDS", "120"))       # ignore vehicle fixes older than this
GPS_MAX_OFFSET = float(env("GPS_MAX_OFFSET_M", "250"))     # bus farther than this from the route = untrusted
GPS_ORIGIN_M = 300.0                                        # within this of the first stop = not departed yet
GPS_PASSED_M = 30.0                                        # bus this far beyond our stop = already passed


def parse_routes(spec):
    """'158:new york,126:hoboken|jersey city' -> {'158': ['new york'], '126': ['hoboken', 'jersey city']}"""
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        route, _, kws = part.partition(":")
        out[route.strip()] = [k.strip().lower() for k in kws.split("|") if k.strip()]
    return out


ROUTES = parse_routes(env("ROUTES", ""))


# ----------------------------------------------------------------------------
# NJT API client
# ----------------------------------------------------------------------------
class Njt:
    def __init__(self, username, password):
        self.username, self.password = username, password
        self.token, self.token_at = None, 0.0
        self.http = requests.Session()
        self.http.headers["accept"] = "*/*"

    def _form(self, endpoint, **fields):
        # API wants multipart/form-data; (None, value) makes requests send plain form parts
        files = {k: (None, v) for k, v in fields.items()}
        r = self.http.post(f"{BASE_URL}/{endpoint}", files=files, timeout=60)
        r.raise_for_status()
        return r.content

    def authenticate(self):
        body = self._form("authenticateUser", username=self.username, password=self.password)
        data = json.loads(body)
        if str(data.get("Authenticated", "")).lower() != "true" or not data.get("UserToken"):
            raise RuntimeError(f"NJT authentication failed: {data.get('errorMessage') or 'check username/password'}")
        self.token, self.token_at = data["UserToken"], time.time()
        log.info("authenticated with NJT API")

    def call(self, endpoint, _retry=True):
        if not self.token or time.time() - self.token_at > TOKEN_MAX_AGE_S:
            self.authenticate()
        body = self._form(endpoint, token=self.token)
        if body[:1] == b"{":  # protobuf/zip never start with '{' -> this is a JSON error
            try:
                msg = json.loads(body).get("errorMessage", "")
            except ValueError:
                msg = body[:200].decode(errors="replace")
            if "token" in msg.lower() and _retry:
                self.token = None
                return self.call(endpoint, _retry=False)
            raise RuntimeError(f"{endpoint}: {msg or 'unexpected JSON response'}")
        return body


# ----------------------------------------------------------------------------
# Static GTFS (only what we need: our stop, our routes)
# ----------------------------------------------------------------------------
class Static:
    def __init__(self):
        self.stop_ids = set()
        self.trips = {}          # trip_id -> {route, headsign, service_id}
        self.at_stop = {}        # trip_id -> [(stop_sequence, secs)] for our stop
        self.seq_secs = {}       # trip_id -> {stop_sequence: secs}
        self.stop_seq = {}       # trip_id -> {stop_id: stop_sequence}
        self.calendar = {}       # service_id -> {start, end, days[7]}
        self.cal_dates = {}      # (service_id, YYYYMMDD) -> exception_type
        self.headsigns = {}      # route -> {headsign: trips serving our stop}
        self.geom = {}           # trip_id -> {"pts": [(lat, lon, secs)], "cum": [metres], "k": [stop indexes]}
        self.loaded_at = time.time()


def _rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def parse_hms(v):
    v = (v or "").strip()
    if not v:
        return None
    h, m, s = v.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _metres(lat1, lon1, lat2, lon2):
    """Small-distance flat-earth distance in metres (fine at city scale)."""
    ky = 110540.0
    kx = 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * ky)


def load_static(path, stop=STOP, routes=ROUTES):
    s = Static()
    xy, tpts = {}, {}
    with zipfile.ZipFile(path) as z:
        for r in _rows(z, "stops.txt"):
            if (r.get("stop_id") or "").strip() == stop or (r.get("stop_code") or "").strip() == stop:
                s.stop_ids.add(r["stop_id"].strip())
            try:
                xy[r["stop_id"].strip()] = (float(r["stop_lat"]), float(r["stop_lon"]))
            except (KeyError, ValueError, TypeError):
                pass
        if not s.stop_ids:
            raise ValueError(f"stop {stop} not found in stops.txt (checked stop_id and stop_code)")

        label = {}
        for r in _rows(z, "routes.txt"):
            rid = (r.get("route_id") or "").strip()
            short = (r.get("route_short_name") or "").strip()
            for want in routes:
                if want in (rid, short):
                    label[rid] = want
        missing = set(routes) - set(label.values())
        if missing:
            raise ValueError(f"routes not found in routes.txt: {sorted(missing)}")

        for r in _rows(z, "trips.txt"):
            rid = (r.get("route_id") or "").strip()
            if rid in label:
                s.trips[r["trip_id"].strip()] = {
                    "route": label[rid],
                    "headsign": (r.get("trip_headsign") or "").strip(),
                    "service_id": (r.get("service_id") or "").strip(),
                }

        for r in _rows(z, "stop_times.txt"):
            tid = (r.get("trip_id") or "").strip()
            if tid not in s.trips:
                continue
            seq = int(r["stop_sequence"])
            sid = (r.get("stop_id") or "").strip()
            secs = parse_hms(r.get("departure_time"))
            if secs is None:
                secs = parse_hms(r.get("arrival_time"))
            if secs is not None:
                s.seq_secs.setdefault(tid, {})[seq] = secs
                if sid in xy:
                    tpts.setdefault(tid, []).append((seq, xy[sid][0], xy[sid][1], secs, sid in s.stop_ids))
            s.stop_seq.setdefault(tid, {})[sid] = seq
            if sid in s.stop_ids and secs is not None:
                s.at_stop.setdefault(tid, []).append((seq, secs))

        names = set(z.namelist())
        if "calendar.txt" in names:
            days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            for r in _rows(z, "calendar.txt"):
                s.calendar[r["service_id"].strip()] = {
                    "start": r["start_date"].strip(),
                    "end": r["end_date"].strip(),
                    "days": [r[d].strip() == "1" for d in days],
                }
        if "calendar_dates.txt" in names:
            for r in _rows(z, "calendar_dates.txt"):
                s.cal_dates[(r["service_id"].strip(), r["date"].strip())] = int(r["exception_type"])

    # Route geometry for GPS estimates: the trip's stops in order form a polyline
    for tid in s.at_stop:
        rows = sorted(tpts.get(tid, []))
        if len(rows) < 2:
            continue
        cum = [0.0]
        for a, b in zip(rows, rows[1:]):
            cum.append(cum[-1] + _metres(a[1], a[2], b[1], b[2]))
        s.geom[tid] = {"pts": [(r[1], r[2], r[3]) for r in rows], "cum": cum,
                       "k": [i for i, r in enumerate(rows) if r[4]]}

    for tid in s.at_stop:
        m = s.trips[tid]
        hs = s.headsigns.setdefault(m["route"], {})
        hs[m["headsign"]] = hs.get(m["headsign"], 0) + 1
    log.info("static GTFS loaded: stop ids %s, %d route trips, %d serve the stop",
             sorted(s.stop_ids), len(s.trips), len(s.at_stop))
    return s


def service_active(s, sid, d):
    ds = d.strftime("%Y%m%d")
    exc = s.cal_dates.get((sid, ds))
    if exc == 1:
        return True
    if exc == 2:
        return False
    c = s.calendar.get(sid)
    return bool(c and c["start"] <= ds <= c["end"] and c["days"][d.weekday()])


def sched_epoch(d, secs):
    """GTFS time: seconds after 'noon minus 12h' of the service date (DST-safe)."""
    base = datetime.combine(d, dtime(12, 0), TZ) - timedelta(hours=12)
    return base.timestamp() + secs


def kw_match(headsign, kws):
    h = headsign.lower()
    return not kws or any(k in h for k in kws)


# ----------------------------------------------------------------------------
# ETA computation
# ----------------------------------------------------------------------------
def _service_date(tu, secs, now):
    sd = tu.trip.start_date if tu.trip.HasField("start_date") else ""
    if len(sd) == 8:
        return date(int(sd[:4]), int(sd[4:6]), int(sd[6:]))
    today = datetime.fromtimestamp(now, TZ).date()
    return min((today, today - timedelta(days=1)), key=lambda d: abs(sched_epoch(d, secs) - now))


def trip_eta(tu, s, now):
    """Best ETA (epoch) for our stop on this trip update, or None. Returns (eta, sched_epoch)."""
    tid = tu.trip.trip_id
    best = None
    for seq, secs in s.at_stop.get(tid, []):
        d = _service_date(tu, secs, now)
        ep = sched_epoch(d, secs)
        eta = None

        # 1) explicit update for our stop
        for stu in tu.stop_time_update:
            if stu.stop_id not in s.stop_ids:
                if not (stu.HasField("stop_sequence") and stu.stop_sequence == seq):
                    continue
            if stu.schedule_relationship == rt.TripUpdate.StopTimeUpdate.SKIPPED:
                continue
            ev = stu.arrival if stu.HasField("arrival") else stu.departure
            if ev.HasField("time") and ev.time > 0:
                eta = float(ev.time)
            elif ev.HasField("delay"):
                eta = ep + ev.delay
            if eta is not None:
                break

        # 2) no explicit update: carry forward the delay from the latest earlier stop
        if eta is None:
            prev = None
            for stu in tu.stop_time_update:
                useq = stu.stop_sequence if stu.HasField("stop_sequence") else s.stop_seq.get(tid, {}).get(stu.stop_id)
                if useq is None or useq >= seq or stu.schedule_relationship == rt.TripUpdate.StopTimeUpdate.SKIPPED:
                    continue
                if prev is None or useq > prev[0]:
                    prev = (useq, stu)
            if prev:
                useq, stu = prev
                ev = stu.arrival if stu.HasField("arrival") else stu.departure
                delay = None
                if ev.HasField("delay"):
                    delay = ev.delay
                elif ev.HasField("time") and ev.time > 0 and useq in s.seq_secs.get(tid, {}):
                    delay = ev.time - sched_epoch(d, s.seq_secs[tid][useq])
                if delay is not None:
                    eta = ep + delay

        if eta is not None and eta >= now - GRACE and (best is None or eta < best[0]):
            best = (eta, ep)
    return best


def _project(geom, lat, lon):
    """Nearest point on the trip's stop polyline -> (offset_m, metres_along_route, scheduled_secs_there)."""
    pts, cum = geom["pts"], geom["cum"]
    ky = 110540.0
    kx = 111320.0 * math.cos(math.radians(lat))
    best = None
    for i in range(len(pts) - 1):
        ax, ay = (pts[i][1] - lon) * kx, (pts[i][0] - lat) * ky
        bx, by = (pts[i + 1][1] - lon) * kx, (pts[i + 1][0] - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 < 1 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if best is None or d < best[0]:
            best = (d, i, t)
    d, i, t = best
    along = cum[i] + t * (cum[i + 1] - cum[i])
    secs = pts[i][2] + t * (pts[i + 1][2] - pts[i][2])
    return d, along, secs


def gps_eta(tid, veh, s, now):
    """Estimate arrival at our stop from a vehicle fix. Returns (status, info).
    status: ok | passed | off_route | stale | no_geometry | no_service_day"""
    geom = s.geom.get(tid)
    if not geom or not geom["k"]:
        return "no_geometry", {}
    lat, lon, ts, vid = veh
    if now - ts > GPS_MAX_AGE:
        return "stale", {"age_s": int(now - ts)}
    d, along, psecs = _project(geom, lat, lon)
    info = {"offset_m": round(d), "vehicle_id": vid}
    if d > GPS_MAX_OFFSET:
        return "off_route", info
    meta = s.trips[tid]
    today = datetime.fromtimestamp(now, TZ).date()
    days = [x for x in (today - timedelta(days=1), today, today + timedelta(days=1))
            if service_active(s, meta["service_id"], x)]
    if not days:
        return "no_service_day", info
    day = min(days, key=lambda x: abs(sched_epoch(x, psecs) - now))
    if abs(sched_epoch(day, psecs) - now) > 3 * 3600:
        return "no_service_day", info
    best = None
    for ki in geom["k"]:
        stop_m = geom["cum"][ki]
        if along > stop_m + GPS_PASSED_M:
            continue
        rem_m = max(0.0, stop_m - along)
        rem_s = max(0.0, geom["pts"][ki][2] - psecs)
        # Timetable pace for the remaining stretch, then assume the bus does better than that:
        # better to arrive early at the stop than to miss the bus.
        pace = max(rem_m / rem_s, GPS_MIN_SPEED_MS) if rem_s > 0 else GPS_MIN_SPEED_MS
        sched_ep = sched_epoch(day, geom["pts"][ki][2])
        eta = now + rem_m / (pace + GPS_SPEEDUP_MS)
        if along < GPS_ORIGIN_M:
            # still sitting at its starting point (layover): can't leave before the timetable says
            eta = max(eta, sched_ep)
        if best is None or eta < best[0]:
            best = (eta, stop_m - along, rem_s, sched_ep)
    if best is None:
        return "passed", info
    eta, rem_m, rem_s, sched_ep = best
    info.update(eta=eta, sched_ep=sched_ep, remaining_m=round(max(0, rem_m)), remaining_min=round(rem_s / 60, 1))
    return "ok", info


def compute(feed, s, routes, now, vehicles=None, gps_log=None):
    est = {r: [] for r in routes}
    stats = {"rt_trips": 0, "rt_matched": 0, "rt_unmatched": 0, "cancelled": 0, "scheduled_added": 0,
             "gps_added": 0, "gps_passed": 0}
    rt_seen, cancelled = set(), set()

    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        tid = tu.trip.trip_id
        stats["rt_trips"] += 1
        meta = s.trips.get(tid)
        if meta is None:
            stats["rt_unmatched"] += 1
            continue
        if not kw_match(meta["headsign"], routes[meta["route"]]) or tid not in s.at_stop:
            continue
        stats["rt_matched"] += 1
        rt_seen.add(tid)
        if tu.trip.schedule_relationship == rt.TripDescriptor.CANCELED:
            cancelled.add(tid)
            stats["cancelled"] += 1
            continue
        got = trip_eta(tu, s, now)
        if got:
            eta, ep = got
            vid = tu.vehicle.id or tu.vehicle.label
            est[meta["route"]].append({
                "eta": eta, "source": "realtime", "headsign": meta["headsign"],
                "delay_min": round((eta - ep) / 60), "vehicle_id": vid or None, "trip_id": tid,
            })

    # Trips that haven't left their origin aren't in the realtime feed -> use the schedule
    today = datetime.fromtimestamp(now, TZ).date()
    for tid, stops in s.at_stop.items():
        if tid in rt_seen or tid in cancelled:
            continue
        meta = s.trips[tid]
        if not kw_match(meta["headsign"], routes[meta["route"]]):
            continue
        if GPS_ENABLED and vehicles and tid in vehicles:
            status, info = gps_eta(tid, vehicles[tid], s, now)
            if gps_log is not None:
                gps_log.append({"trip_id": tid, "route": meta["route"], "headsign": meta["headsign"],
                                "status": status, **{k: v for k, v in info.items() if k not in ("eta", "sched_ep")},
                                "eta": iso(info["eta"]) if "eta" in info else None,
                                "sched": iso(info["sched_ep"]) if "sched_ep" in info else None})
            if status == "ok":
                est[meta["route"]].append({
                    "eta": info["eta"], "source": "gps", "headsign": meta["headsign"],
                    "delay_min": round((info["eta"] - info["sched_ep"]) / 60),
                    "vehicle_id": info.get("vehicle_id"), "trip_id": tid,
                })
                stats["gps_added"] += 1
                continue
            if status == "passed":
                stats["gps_passed"] += 1
                continue  # bus already went by; its timetable slot is not a real upcoming bus
        for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
            if not service_active(s, meta["service_id"], d):
                continue
            for _seq, secs in stops:
                ep = sched_epoch(d, secs)
                if now - GRACE <= ep <= now + SCHEDULE_HORIZON:
                    est[meta["route"]].append({
                        "eta": ep, "source": "scheduled", "headsign": meta["headsign"],
                        "delay_min": None, "vehicle_id": None, "trip_id": tid,
                    })
                    stats["scheduled_added"] += 1
    for r in est:
        est[r].sort(key=lambda e: e["eta"])
    return est, stats


# ----------------------------------------------------------------------------
# State + HTTP
# ----------------------------------------------------------------------------
LOCK = threading.Lock()
STATE = {"estimates": {}, "last_ok": 0.0, "status": "starting", "error": None, "stats": {}, "static": None,
         "gps": []}


def iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


def minutes_until(eta, now):
    return max(0, math.ceil((eta - now) / 60))


def route_payload(route, now=None):
    now = now or time.time()
    with LOCK:
        ests = list(STATE["estimates"].get(route, []))
        last_ok, status, error = STATE["last_ok"], STATE["status"], STATE["error"]
    out = {"route": route, "stop": STOP, "minutes": None, "eta": None, "source": None, "headsign": None,
           "delay_min": None, "vehicle_id": None, "trip_id": None, "next": [],
           "updated": iso(last_ok) if last_ok else None, "status": status, "error": error}
    if not last_ok:
        return out
    if now - last_ok > STALE_AFTER:
        out["status"] = "stale"  # never show a frozen number as if it were live
        return out
    live = [e for e in ests if e["eta"] >= now - GRACE]
    if not live:
        out["status"] = "no_upcoming"
        return out
    first, rest = live[0], live[1:4]
    out.update({
        "minutes": minutes_until(first["eta"], now), "eta": iso(first["eta"]), "source": first["source"],
        "headsign": first["headsign"], "delay_min": first["delay_min"], "vehicle_id": first["vehicle_id"],
        "trip_id": first["trip_id"],
        "next": [{"minutes": minutes_until(e["eta"], now), "eta": iso(e["eta"]), "source": e["source"]} for e in rest],
    })
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/api/route/"):
            route = path.rsplit("/", 1)[1]
            if route not in ROUTES:
                return self._send(404, {"error": f"route {route} not configured", "configured": sorted(ROUTES)})
            return self._send(200, route_payload(route))
        if path == "/api/next":
            return self._send(200, {r: route_payload(r) for r in ROUTES})
        if path == "/healthz":
            ok = STATE["last_ok"] and time.time() - STATE["last_ok"] <= STALE_AFTER
            return self._send(200 if ok else 503, {"ok": bool(ok), "status": STATE["status"], "error": STATE["error"]})
        if path == "/debug":
            st = STATE["static"]
            return self._send(200, {
                "stop": STOP, "routes": ROUTES, "status": STATE["status"], "error": STATE["error"],
                "last_ok": iso(STATE["last_ok"]) if STATE["last_ok"] else None, "stats": STATE["stats"],
                "stop_ids_matched": sorted(st.stop_ids) if st else None,
                "gps_candidates": STATE["gps"],
                "headsigns_seen_at_stop": st.headsigns if st else None,
                "hint": "If a route shows no headsigns or the wrong ones, edit ROUTES keywords in .env",
            })
        self._send(404, {"error": "not found"})

    def log_message(self, *_a):
        pass


# ----------------------------------------------------------------------------
# Poller
# ----------------------------------------------------------------------------
def refresh_static(njt, old):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "gtfs_static.zip")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < STATIC_REFRESH_S
    try:
        if not fresh:
            log.info("downloading static GTFS (getGTFS)...")
            data = njt.call("getGTFS")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return load_static(path)
    except Exception:
        if old is not None:
            log.exception("static refresh failed; keeping previous schedule, retrying in 1h")
            old.loaded_at = time.time() - STATIC_REFRESH_S + 3600
            return old
        raise


def fetch_vehicles(njt):
    """trip_id -> (lat, lon, timestamp, vehicle_id). Failure only disables GPS for this cycle."""
    try:
        vp = rt.FeedMessage()
        vp.ParseFromString(njt.call("getVehiclePositions"))
        out = {}
        for e in vp.entity:
            if e.HasField("vehicle") and e.vehicle.trip.trip_id and e.vehicle.HasField("position"):
                v = e.vehicle
                out[v.trip.trip_id] = (v.position.latitude, v.position.longitude,
                                       v.timestamp or time.time(), v.vehicle.id or v.vehicle.label or None)
        return out
    except Exception as ex:
        log.warning("vehicle positions unavailable this cycle: %s", ex)
        return None


def poller(njt):
    static = None
    while True:
        try:
            if static is None or time.time() - static.loaded_at > STATIC_REFRESH_S:
                static = refresh_static(njt, static)
                with LOCK:
                    STATE["static"] = static
            feed = rt.FeedMessage()
            feed.ParseFromString(njt.call("getTripUpdates"))
            vehicles = fetch_vehicles(njt) if GPS_ENABLED else None
            now = time.time()
            gps_log = []
            est, stats = compute(feed, static, ROUTES, now, vehicles, gps_log)
            with LOCK:
                STATE.update(estimates=est, stats=stats, last_ok=now, status="ok", error=None, gps=gps_log)
        except Exception as e:  # keep serving last good data (marked stale after STALE_AFTER)
            log.exception("poll failed")
            with LOCK:
                STATE.update(status="error", error=str(e)[:300])
            if static is None:
                time.sleep(60)
        time.sleep(POLL_SECONDS)


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    user, pw = env("NJT_USERNAME"), env("NJT_PASSWORD")
    if not user or not pw:
        raise SystemExit("NJT_USERNAME and NJT_PASSWORD must be set (see .env.example)")
    if not STOP or not ROUTES:
        raise SystemExit("STOP_ID and ROUTES must be set (see .env.example)")
    log.info("stop %s, routes %s, poll every %ss", STOP, ROUTES, POLL_SECONDS)
    threading.Thread(target=poller, args=(Njt(user, pw),), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
