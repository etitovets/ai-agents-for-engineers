"""Response schemas exposed by the weather proxy."""

from datetime import date as date_type
from typing import Literal

from pydantic import BaseModel, Field


class CurrentWeather(BaseModel):
    """Normalised current conditions for one location."""

    latitude: float = Field(description="Latitude in decimal degrees, as requested.")
    longitude: float = Field(description="Longitude in decimal degrees, as requested.")
    temperature: float = Field(
        description="Air temperature at 2 m above ground, in degrees Celsius."
    )
    wind_speed: float = Field(description="Wind speed at 10 m above ground, in km/h.")
    source: Literal["open-meteo"] = "open-meteo"


class DailyForecast(BaseModel):
    """Normalised daily forecast for a single day."""

    date: date_type = Field(
        description="Calendar date of the forecast, in the location's time zone."
    )
    temperature_max: float = Field(
        description="Maximum air temperature at 2 m, in degrees Celsius."
    )
    temperature_min: float = Field(
        description="Minimum air temperature at 2 m, in degrees Celsius."
    )
    precipitation_sum: float = Field(description="Total precipitation during the day, in mm.")
    weather_code: int = Field(description="WMO weather interpretation code.")


class ForecastResponse(BaseModel):
    """Daily forecast for one location."""

    latitude: float = Field(description="Latitude in decimal degrees, as requested.")
    longitude: float = Field(description="Longitude in decimal degrees, as requested.")
    days: int = Field(description="Number of forecast days requested.")
    forecast: list[DailyForecast] = Field(description="One entry per forecast day.")
    source: Literal["open-meteo"] = "open-meteo"
