"""Thin async client for Open-Meteo that normalises its responses."""

from typing import Any

import httpx
from pydantic import ValidationError

from models import CurrentWeather, DailyForecast, ForecastResponse

CURRENT_FIELDS = "temperature_2m,wind_speed_10m"
DAILY_FIELDS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"

DAILY_KEYS = (
    "time",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "weather_code",
)


class UpstreamError(Exception):
    """The upstream service failed or answered with something unusable."""

    def __init__(self, code: str, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.status_code = status_code
        self.detail = detail


class OpenMeteoClient:
    """Fetches Open-Meteo data and converts it into local models."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout: float) -> None:
        self._client = client
        self._base_url = base_url
        self._timeout = timeout

    async def current(self, *, latitude: float, longitude: float) -> CurrentWeather:
        """Return normalised current conditions for the given point."""
        payload = await self._get_json(
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": CURRENT_FIELDS,
            }
        )
        current = self._mapping(payload, "current")
        return CurrentWeather(
            latitude=latitude,
            longitude=longitude,
            temperature=self._number(current, "temperature_2m", "current"),
            wind_speed=self._number(current, "wind_speed_10m", "current"),
        )

    async def forecast(self, *, latitude: float, longitude: float, days: int) -> ForecastResponse:
        """Return a normalised daily forecast for the given point."""
        payload = await self._get_json(
            {
                "latitude": latitude,
                "longitude": longitude,
                "daily": DAILY_FIELDS,
                "forecast_days": days,
                "timezone": "auto",
            }
        )
        daily = self._mapping(payload, "daily")
        columns = {key: self._sequence(daily, key, "daily") for key in DAILY_KEYS}
        lengths = {key: len(values) for key, values in columns.items()}
        if len(set(lengths.values())) != 1:
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                f"Open-Meteo returned daily columns of different lengths: {lengths}.",
            )

        try:
            entries = [
                DailyForecast(
                    date=columns["time"][index],
                    temperature_max=columns["temperature_2m_max"][index],
                    temperature_min=columns["temperature_2m_min"][index],
                    precipitation_sum=columns["precipitation_sum"][index],
                    weather_code=columns["weather_code"][index],
                )
                for index in range(len(columns["time"]))
            ]
        except ValidationError as exc:
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                "Open-Meteo returned daily values in an unexpected format.",
            ) from exc

        return ForecastResponse(
            latitude=latitude,
            longitude=longitude,
            days=days,
            forecast=entries,
        )

    async def _get_json(self, params: dict[str, Any]) -> dict[str, Any]:
        """Call Open-Meteo and return its JSON object."""
        try:
            response = await self._client.get(
                self._base_url,
                params=params,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                "upstream_timeout",
                504,
                f"Open-Meteo did not respond within {self._timeout:g}s.",
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "upstream_unavailable",
                502,
                f"Could not reach Open-Meteo: {exc}",
            ) from exc

        if response.is_error:
            raise UpstreamError("upstream_error", 502, self._describe_error(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                "Open-Meteo returned a response that is not valid JSON.",
            ) from exc

        if not isinstance(payload, dict):
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                "Open-Meteo returned JSON that is not an object.",
            )
        return payload

    @staticmethod
    def _describe_error(response: httpx.Response) -> str:
        """Build a readable message from an unsuccessful upstream response."""
        reason: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            raw_reason = body.get("reason") or body.get("error")
            if isinstance(raw_reason, str) and raw_reason:
                reason = raw_reason
        elif not body:
            reason = response.text.strip()[:200] or None

        message = f"Open-Meteo responded with HTTP {response.status_code}"
        return f"{message}: {reason}" if reason else f"{message}."

    @staticmethod
    def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
        """Return `payload[key]` if it is a JSON object."""
        value = payload.get(key)
        if not isinstance(value, dict):
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                f"Open-Meteo response is missing the '{key}' object.",
            )
        return value

    @staticmethod
    def _number(mapping: dict[str, Any], key: str, section: str) -> float:
        """Return `mapping[key]` as a float."""
        value = mapping.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                f"Expected a number at '{section}.{key}' in the Open-Meteo response.",
            )
        return float(value)

    @staticmethod
    def _sequence(mapping: dict[str, Any], key: str, section: str) -> list[Any]:
        """Return `mapping[key]` if it is a JSON array."""
        value = mapping.get(key)
        if not isinstance(value, list):
            raise UpstreamError(
                "invalid_upstream_response",
                502,
                f"Expected a list at '{section}.{key}' in the Open-Meteo response.",
            )
        return value
