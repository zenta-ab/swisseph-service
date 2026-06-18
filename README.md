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

## Ephemeris data

The main bodies (Sun..Pluto and the lunar nodes) are computed via the built-in
Moshier ephemeris, so no large data files are required. Chiron has no Moshier
fallback, so its asteroid ephemeris (`ephe/seas_18.se1`, ~220 KB) ships with
the service.

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
