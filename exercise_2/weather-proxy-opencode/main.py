"""FastAPI weather proxy in front of the Open-Meteo API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import get_settings
from models import CurrentWeather, ForecastResponse
from open_meteo import OpenMeteoClient, UpstreamError

Latitude = Annotated[
    float,
    Query(
        ge=-90,
        le=90,
        description="Latitude in decimal degrees.",
        examples=[41.7151],
    ),
]
Longitude = Annotated[
    float,
    Query(
        ge=-180,
        le=180,
        description="Longitude in decimal degrees.",
        examples=[44.8271],
    ),
]
Days = Annotated[
    int,
    Query(
        ge=1,
        le=7,
        description="Number of forecast days to return (1-7).",
        examples=[3],
    ),
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create one shared HTTP client for the lifetime of the application."""
    settings = get_settings()
    async with httpx.AsyncClient(
        headers={"User-Agent": "weather-proxy/0.1"},
        timeout=httpx.Timeout(settings.request_timeout),
    ) as client:
        app.state.open_meteo = OpenMeteoClient(
            client=client,
            base_url=settings.open_meteo_base_url,
            timeout=settings.request_timeout,
        )
        yield


app = FastAPI(
    title="Weather proxy",
    version="0.1.0",
    summary="Normalised current weather and daily forecast from Open-Meteo.",
    lifespan=lifespan,
)


def get_open_meteo(request: Request) -> OpenMeteoClient:
    """Return the Open-Meteo client stored in the application state."""
    return request.app.state.open_meteo


OpenMeteoDep = Annotated[OpenMeteoClient, Depends(get_open_meteo)]


@app.get("/health", tags=["system"], summary="Liveness probe")
async def health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok"}


@app.get(
    "/weather",
    response_model=CurrentWeather,
    tags=["weather"],
    summary="Current weather for a point",
)
async def weather(
    latitude: Latitude,
    longitude: Longitude,
    upstream: OpenMeteoDep,
) -> CurrentWeather:
    """Proxy a current-conditions request to Open-Meteo and normalise the answer."""
    return await upstream.current(latitude=latitude, longitude=longitude)


@app.get(
    "/forecast",
    response_model=ForecastResponse,
    tags=["weather"],
    summary="Daily forecast for a point",
)
async def forecast(
    latitude: Latitude,
    longitude: Longitude,
    upstream: OpenMeteoDep,
    days: Days = 3,
) -> ForecastResponse:
    """Return a daily forecast for one to seven days ahead."""
    return await upstream.forecast(latitude=latitude, longitude=longitude, days=days)


@app.exception_handler(UpstreamError)
async def handle_upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
    """Translate Open-Meteo failures into a uniform JSON error body."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.detail},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Report invalid query parameters as one readable message."""
    problems: list[dict[str, Any]] = []
    messages: list[str] = []
    for issue in exc.errors():
        location = ".".join(str(part) for part in issue["loc"] if part != "query") or "request"
        messages.append(f"{location}: {issue['msg']}")
        problems.append({"field": location, "message": issue["msg"], "type": issue["type"]})
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "detail": "; ".join(messages),
            "problems": problems,
        },
    )
