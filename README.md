# njt-bus: NJ Transit bus ETA sidecar for Home Assistant

Small Python service that polls the NJ Transit Bus GTFS-RT G2 API every 30s and
serves clean "minutes until next bus" JSON for one stop, ready to drop into
Home Assistant (and Apple Home via HomeKit Bridge).

Each bus gets one of three sources:
- `realtime`: NJT trip update with a prediction for the stop.
- `gps`: no NJT prediction, so the ETA is estimated from the bus's GPS position:
  timetable pace for the remaining stretch, assumed `GPS_SPEEDUP_MPH` faster (default 5)
  so the estimate errs early. Buses past the stop are dropped; buses still at their
  origin never show earlier than the timetable.
- `scheduled`: timetable only (not yet departed / no usable GPS).

## Requirements
- Docker + Docker Compose
- NJ Transit developer API credentials: register at <https://developer.njtransit.com>

## Run
    cp .env.example .env    # then fill in credentials, STOP_ID and ROUTES
    docker compose up -d --build
    curl localhost:8085/api/next        # all routes
    curl localhost:8085/api/route/158   # one route
    curl localhost:8085/debug           # headsigns seen at the stop + GPS candidates

Direction is filtered by headsign keyword (`ROUTES=158:new york,126:hoboken`); check
`/debug` if a route looks wrong, edit `ROUTES` in `.env`, then `docker compose up -d`.
The stop and route values in this repo are examples; replace them with your own.

## Configuration
| Variable | Default | Meaning |
| --- | --- | --- |
| `NJT_USERNAME` / `NJT_PASSWORD` | (required) | NJT developer API credentials |
| `STOP_ID` | (required) | Stop ID or stop code |
| `ROUTES` | (required) | `route:keyword\|keyword,...` |
| `POLL_SECONDS` | `30` | Realtime poll interval |
| `GPS_ESTIMATE` | `1` | `0` disables GPS-based estimates |
| `GPS_SPEEDUP_MPH` | `5` | Optimism added to timetable pace for GPS estimates |
| `STALE_AFTER_SECONDS` | `180` | Mark data stale after this long without a good poll |
| `SCHEDULE_HORIZON_MINUTES` | `180` | How far ahead to include scheduled trips |
| `STATIC_REFRESH_HOURS` | `24` | Static GTFS refresh interval |

## Networking
Port `8085` is published on all interfaces, so Home Assistant reaches it at
`http://<host-ip>:8085`. The API is read-only, unauthenticated and exposes no secrets;
keep it off the internet. The sidecar needs outbound HTTPS to `pcsdata.njtransit.com`.

## Output
`status`: `ok` | `stale` (no good poll for 3 min; minutes is null) | `no_upcoming` |
`error` | `starting`. `minutes` is rounded up.

## Home Assistant (homeassistant/)
- `njt_bus_package.yaml`: REST sensors `sensor.next_158_in` / `sensor.next_126_in` + recorder exclude.
  Edit the `resource:` URL to point at your sidecar host.
- `dashboard_cards.yaml`: Markdown card (red <=5, yellow 6, green >=7, "(sched)"/"(est)" tags)
- `homekit.yaml`: lux-proxy sensors for Apple Home

## License
MIT, see [LICENSE](LICENSE).
