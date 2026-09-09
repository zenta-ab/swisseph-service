# swisseph-service

A small FastAPI microservice that exposes professional-grade astronomical
calculations over HTTP, backed by the
[Swiss Ephemeris](https://www.astro.com/swisseph/).

It computes sidereal planetary positions, houses and ayanamsa, the Human
Design "design" date (88 degrees of solar arc before birth), and
astrocartography lines (MC / IC / ASC / DSC per planet).

## Endpoints

| Method | Path           | Description                                                 |
|--------|----------------|-------------------------------------------------------------|
| GET    | `/health`      | Health check                                                |
| POST   | `/calculate`   | Planets, houses, ayanamsa, nakshatra (sidereal)             |
| POST   | `/design-date` | Moment 88 degrees of solar arc before the birth Sun         |
| POST   | `/astrolines`  | Astrocartography MC / IC / ASC / DSC lines per planet       |

## Running

### Docker

```sh
docker build -t swisseph-service .
docker run -p 9047:9047 swisseph-service
```

### Local

```sh
pip install -r requirements.txt
python main.py            # serves on http://127.0.0.1:9047
```

Configuration via environment variables:

- `SWISSEPH_PORT` - listen port (default `9047`)
- `SWISSEPH_EPHE_PATH` - path to the ephemeris data files (default `./ephe`)
- `SWISSEPH_CORS_ORIGINS` - comma-separated browser origins to allow.
  Empty by default: the service is meant to be called server-to-server over a
  private network, where CORS does not apply.
- `SWISSEPH_REQUIRE_FULL_PRECISION` - default `true`. Decides whether running on
  the Moshier fallback is reported as `failed` (with an ERROR at boot) or as
  `degraded` (a WARNING). Either way the service keeps serving; see below.
- `SWISSEPH_LOG_LEVEL` - default `INFO`.

## Ephemeris data

Only `ephe/seas_18.se1` (~220 KB, the asteroid file Chiron needs) ships with the
service. The main bodies (Sun..Pluto and the lunar nodes) therefore fall back to
the built-in Moshier analytical ephemeris, which needs no data files but is
coarser than the Swiss Ephemeris files.

That fallback is silent by design in the underlying library: `swe.calc_ut` with
`FLG_SWIEPH` does not raise when `sepl_18.se1` / `semo_18.se1` are absent, it
just returns Moshier positions that look exactly as plausible. To make it
visible, the service computes a known instant at startup and reads back the
flags the library actually used, then logs the result and reports it on
`/health`:

```json
{
  "status": "ok",
  "engine": "Swiss Ephemeris",
  "version": "2.10.03",
  "ephemeris": {
    "mode": "moshier",
    "status": "failed",
    "precision_ok": false,
    "path": "/app/ephe",
    "files_missing": ["sepl_18.se1", "semo_18.se1"]
  }
}
```

For full precision, put `sepl_18.se1` and `semo_18.se1` in the ephemeris
directory (bake them into the image or mount them) and restart; `mode` then
reads `swiss`. Reduced precision never stops the service: callers commonly
degrade gracefully when it is unreachable, and exiting would turn a precision
issue into an outage. It is made loud instead.

## License

This service uses the Swiss Ephemeris, which is dual-licensed by Astrodienst AG
under the GNU Affero General Public License v3 (AGPL-3.0) **or** a commercial
Swiss Ephemeris Professional License.

Because it builds on the AGPL-licensed `pyswisseph`, this service is
distributed under the **AGPL-3.0** (see [`LICENSE`](LICENSE)). Under the AGPL,
if you run a modified version of this service and let users interact with it
over a network, you must offer those users access to the corresponding source.

Swiss Ephemeris is Copyright (C) Astrodienst AG, Switzerland. See
<https://www.astro.com/swisseph/> for details and commercial licensing options.
