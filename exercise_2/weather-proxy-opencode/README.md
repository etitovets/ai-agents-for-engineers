# Weather proxy

A small FastAPI service that proxies the [Open-Meteo](https://open-meteo.com/en/docs) forecast API
and returns a compact, normalised JSON payload. It validates coordinates and forecast length,
handles upstream failures with meaningful status codes, and exposes an OpenAPI schema at `/docs`.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync                     # create .venv and install dependencies
cp .env.example .env        # optional: override the defaults
```

## Run

```bash
uv run uvicorn main:app --reload
```

The service listens on <http://127.0.0.1:8000>; interactive docs live at `/docs`.

## Endpoints

### `GET /health`

Liveness probe.

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

### `GET /weather`

Current conditions for a point. Query parameters:

| Name | Type | Range | Required |
| --- | --- | --- | --- |
| `latitude` | float | -90 … 90 | yes |
| `longitude` | float | -180 … 180 | yes |

```bash
curl "http://127.0.0.1:8000/weather?latitude=41.7151&longitude=44.8271"
```

```json
{
  "latitude": 41.7151,
  "longitude": 44.8271,
  "temperature": 28.4,
  "wind_speed": 12.1,
  "source": "open-meteo"
}
```

`temperature` is the air temperature at 2 m in °C, `wind_speed` the wind speed at 10 m in km/h.
Coordinates are echoed exactly as requested.

### `GET /forecast`

Daily forecast for 1 to 7 days. Query parameters:

| Name | Type | Range | Default |
| --- | --- | --- | --- |
| `latitude` | float | -90 … 90 | required |
| `longitude` | float | -180 … 180 | required |
| `days` | int | 1 … 7 | `3` |

```bash
curl "http://127.0.0.1:8000/forecast?latitude=41.7151&longitude=44.8271&days=3"
```

```json
{
  "latitude": 41.7151,
  "longitude": 44.8271,
  "days": 3,
  "forecast": [
    {
      "date": "2026-11-15",
      "temperature_max": 28.4,
      "temperature_min": 18.2,
      "precipitation_sum": 0.0,
      "weather_code": 1
    }
  ],
  "source": "open-meteo"
}
```

Days are resolved in the location's own time zone (`timezone=auto` on the upstream call).

## Configuration

Settings are read from environment variables or a `.env` file:

| Variable | Default | Description |
| --- | --- | --- |
| `OPEN_METEO_BASE_URL` | `https://api.open-meteo.com/v1/forecast` | Upstream endpoint to call |
| `REQUEST_TIMEOUT` | `10` | Timeout in seconds for a single upstream request |

## Error handling

Every error is returned as `{"error": "<code>", "detail": "<message>"}`.

| Status | `error` | When |
| --- | --- | --- |
| 422 | `invalid_request` | Query parameters missing, out of range, or not a number |
| 502 | `upstream_unavailable` | Open-Meteo cannot be reached (DNS, connection, TLS) |
| 502 | `upstream_error` | Open-Meteo answered with a non-2xx status |
| 502 | `invalid_upstream_response` | Response is not JSON, misses fields, or has unexpected types |
| 504 | `upstream_timeout` | Upstream request exceeded `REQUEST_TIMEOUT` |

Example:

```bash
curl -i "http://127.0.0.1:8000/weather?latitude=999&longitude=0"
# HTTP/1.1 422 Unprocessable Entity
# {"error":"invalid_request","detail":"latitude: Input should be less than or equal to 90", ...}
```

## Project layout

```
main.py          FastAPI app, routes, lifespan, exception handlers
config.py        Settings (pydantic-settings, .env aware)
models.py        Response schemas
open_meteo.py    Async Open-Meteo client and response normalisation
```

## Development

```bash
uv run ruff format .
uv run ruff check .
```
