from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Adcode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class CoordinateSystem(StrEnum):
    GCJ02 = "GCJ02"
    WGS84 = "WGS84"
    BD09 = "BD09"
    MAPBAR = "MAPBAR"


class RouteMode(StrEnum):
    WALKING = "walking"
    DRIVING = "driving"
    TRANSIT = "transit"
    BICYCLING = "bicycling"
    ELECTRIC_BIKE = "electric_bike"


class MatrixMode(StrEnum):
    WALKING = "walking"
    DRIVING = "driving"


class AmapModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AmapCoordinateInput(AmapModel):
    longitude: float = Field(ge=-180, le=180, description="Longitude in decimal degrees.")
    latitude: float = Field(ge=-90, le=90, description="Latitude in decimal degrees.")
    coordinate_system: Literal[CoordinateSystem.GCJ02] = Field(
        default=CoordinateSystem.GCJ02,
        description="Amap route and search inputs must use GCJ-02 coordinates.",
    )


class AmapCoordinate(AmapModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    coordinate_system: Literal[CoordinateSystem.GCJ02] = CoordinateSystem.GCJ02
    source: Literal["amap", "amap_conversion"] = "amap"


class CurrentCityInput(AmapModel):
    """No model-authored fields: the client IP comes from trusted request context."""


class CurrentCityResult(AmapModel):
    province: str | None = None
    city: str | None = None
    adcode: str | None = None
    source: Literal["ip"] = "ip"
    accuracy_level: Literal["city", "unavailable"]
    is_estimated: Literal[True] = True
    locatable: bool
    unavailable_reason: str | None = None


class SearchPlacesInput(AmapModel):
    keywords: NonEmptyString = Field(description="POI name or search keywords.")
    city: NonEmptyString | None = Field(
        default=None,
        description="Optional city name used to constrain the search.",
    )
    adcode: Adcode | None = Field(
        default=None,
        description="Preferred six-digit administrative code used to constrain the search.",
    )
    location: AmapCoordinateInput | None = Field(
        default=None,
        description="Optional GCJ-02 center point for a nearby search.",
    )
    radius_meters: int = Field(
        default=3000,
        ge=0,
        le=50_000,
        description="Nearby-search radius in meters; ignored without location.",
    )
    poi_type: NonEmptyString | None = Field(
        default=None,
        description="Optional Amap POI type code or exact provider category name.",
    )
    limit: int = Field(default=10, ge=1, le=25, description="Maximum number of POIs.")


class AmapPlace(AmapModel):
    poi_id: str
    parent_poi_id: str | None = None
    name: str
    address: str
    province: str
    city: str
    district: str
    adcode: str
    poi_type: str
    rating: float | None = Field(default=None, ge=0, le=5)
    business_area: str | None = None
    distance_meters: int | None = Field(default=None, ge=0)
    location: AmapCoordinate


class PlaceSearchResult(AmapModel):
    pois: list[AmapPlace]


class RoutePlanInput(AmapModel):
    origin: AmapCoordinateInput
    destination: AmapCoordinateInput
    mode: RouteMode
    city: NonEmptyString | None = Field(
        default=None,
        description="Required origin city name or citycode for public transit.",
    )
    destination_city: NonEmptyString | None = Field(
        default=None,
        description="Optional destination city for cross-city public transit.",
    )
    strategy: int | None = Field(
        default=None,
        ge=0,
        le=99,
        description="Optional Amap strategy code for the selected route mode.",
    )
    waypoints: tuple[AmapCoordinateInput, ...] = Field(
        default=(),
        max_length=16,
        description="Optional driving-only intermediate points.",
    )

    @model_validator(mode="after")
    def validate_mode_specific_fields(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must be different")
        if self.mode == RouteMode.TRANSIT and not self.city:
            raise ValueError("city is required for transit routes")
        if self.waypoints and self.mode != RouteMode.DRIVING:
            raise ValueError("waypoints are supported only for driving routes")
        return self


class RouteStep(AmapModel):
    instruction: str
    transport: str
    road: str = ""
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    polyline: str | None = None


class RouteResult(AmapModel):
    mode: RouteMode
    distance_meters: int = Field(ge=0)
    duration_seconds: int = Field(ge=0)
    route_summary: str
    steps: list[RouteStep]
    transfers: int | None = Field(default=None, ge=0)
    walking_distance_meters: int | None = Field(default=None, ge=0)
    taxi_cost: float | None = Field(default=None, ge=0)
    polyline: str | None = None


class MatrixLocation(AmapModel):
    id: NonEmptyString = Field(max_length=100)
    name: NonEmptyString = Field(max_length=200)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    coordinate_system: Literal[CoordinateSystem.GCJ02] = CoordinateSystem.GCJ02


class TravelTimeMatrixInput(AmapModel):
    locations: list[MatrixLocation] = Field(min_length=2, max_length=20)
    mode: MatrixMode = MatrixMode.DRIVING

    @model_validator(mode="after")
    def validate_location_ids(self) -> Self:
        ids = [item.id for item in self.locations]
        if len(ids) != len(set(ids)):
            raise ValueError("matrix location ids must be unique")
        return self


class MatrixEntry(AmapModel):
    origin_id: str
    destination_id: str
    success: bool
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.success:
            if self.distance_meters is None or self.duration_seconds is None:
                raise ValueError("successful matrix entries require distance and duration")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful matrix entries cannot contain errors")
        elif not self.error_message:
            raise ValueError("failed matrix entries require an error message")
        return self


class TravelTimeMatrixResult(AmapModel):
    mode: MatrixMode
    locations: list[MatrixLocation]
    matrix: list[MatrixEntry]


class WeatherInput(AmapModel):
    city: NonEmptyString | None = Field(
        default=None,
        description="City name used only when an adcode is not available.",
    )
    adcode: Adcode | None = Field(
        default=None,
        description="Preferred six-digit administrative code.",
    )
    forecast: bool = Field(
        default=True,
        description="Also retrieve the provider's multi-day forecast.",
    )

    @model_validator(mode="after")
    def validate_location(self) -> Self:
        if not self.city and not self.adcode:
            raise ValueError("city or adcode is required")
        return self


class CurrentWeather(AmapModel):
    weather: str
    temperature: str
    humidity: str
    wind_direction: str
    wind_power: str
    report_time: str


class WeatherForecast(AmapModel):
    date: date
    day_weather: str
    night_weather: str
    day_temperature: str
    night_temperature: str
    day_wind_direction: str
    night_wind_direction: str
    day_wind_power: str
    night_wind_power: str


class WeatherResult(AmapModel):
    city: str
    adcode: str
    province: str
    current: CurrentWeather
    forecast: list[WeatherForecast]


class GeocodeResult(AmapModel):
    formatted_address: str
    province: str
    city: str
    district: str
    adcode: str
    citycode: str
    location: AmapCoordinate


class ReverseGeocodeResult(AmapModel):
    formatted_address: str
    province: str
    city: str
    district: str
    adcode: str
    nearby_pois: list[AmapPlace]


class ConvertCoordinateInput(AmapModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    source_coordinate_system: Literal[
        CoordinateSystem.WGS84,
        CoordinateSystem.BD09,
        CoordinateSystem.MAPBAR,
        CoordinateSystem.GCJ02,
    ]


class AmapErrorCode(StrEnum):
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    REQUEST_ERROR = "REQUEST_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    EMPTY_RESULT = "EMPTY_RESULT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


AmapToolData = (
    CurrentCityResult | PlaceSearchResult | RouteResult | TravelTimeMatrixResult | WeatherResult
)


class AmapToolResult(AmapModel):
    success: bool
    provider: Literal["amap"] = "amap"
    data: AmapToolData | None = None
    error_code: AmapErrorCode | None = None
    error_message: str | None = None
    provider_error_code: str | None = Field(default=None, max_length=100)
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.success:
            if self.data is None:
                raise ValueError("successful Amap tool results require data")
            if (
                self.error_code is not None
                or self.error_message is not None
                or self.provider_error_code is not None
            ):
                raise ValueError("successful Amap tool results cannot contain errors")
        else:
            if self.data is not None:
                raise ValueError("failed Amap tool results cannot contain data")
            if self.error_code is None or not self.error_message:
                raise ValueError("failed Amap tool results require an error code and message")
        return self
