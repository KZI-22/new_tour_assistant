from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Hour = Annotated[int, Field(ge=0, le=23)]
PositiveNumber = Annotated[float, Field(gt=0)]
JourneyType = Literal[1, 2]
SortType = Literal[1, 2, 3, 4, 5, 6, 7, 8]
FlightSeatClass = Literal["economy", "business", "first"]
TrainSeatClass = Literal[
    "second class",
    "first class",
    "business class",
    "hard sleeper",
    "soft sleeper",
]
HotelType = Literal["hotel", "homestay", "inn"]
HotelSort = Literal[
    "distance_asc",
    "rate_desc",
    "price_asc",
    "price_desc",
    "no_rank",
]
HotelBedType = Literal["king", "twin", "multi"]
PoiCategory = Literal[
    "nature",
    "lake",
    "forest",
    "canyon",
    "beach",
    "island",
    "desert",
    "grassland",
    "historic site",
    "ancient town",
    "garden",
    "temple",
    "theme park",
    "water park",
    "zoo",
    "aquarium",
    "museum",
    "memorial",
    "landmark",
    "market",
    "outdoor",
    "skiing",
    "rafting",
    "surfing",
    "diving",
    "camping",
    "hot spring",
]


class TravelSearchInput(BaseModel):
    """Shared validation policy for model-authored travel search inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TextSearchInput(TravelSearchInput):
    query: NonEmptyString = Field(description="Natural-language FlyAI search query.")


class TransportSearchInput(TravelSearchInput):
    origin: NonEmptyString = Field(description="Origin city, station, or airport name/ID.")
    destination: NonEmptyString = Field(description="Destination city, station, or airport.")
    departure_date: date = Field(description="Departure date in YYYY-MM-DD format.")
    return_date: date | None = Field(
        default=None,
        description="Optional return date in YYYY-MM-DD format.",
    )
    journey_type: JourneyType | None = Field(
        default=None,
        description="1 for direct service, 2 for a transfer itinerary.",
    )
    transfer_cities: tuple[NonEmptyString, ...] = ()
    departure_hour_start: Hour | None = None
    departure_hour_end: Hour | None = None
    arrival_hour_start: Hour | None = None
    arrival_hour_end: Hour | None = None
    max_duration_hours: PositiveNumber | None = None
    max_price: PositiveNumber | None = Field(
        default=None,
        description="Maximum total price in CNY.",
    )
    sort_type: SortType | None = Field(
        default=None,
        description=(
            "1 price-desc, 2 recommended, 3 price-asc, 4 duration-asc, "
            "5 duration-desc, 6 early departure, 7 late departure, 8 direct-first."
        ),
    )

    @model_validator(mode="after")
    def validate_route_and_dates(self) -> Self:
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError("origin and destination must be different")
        if self.departure_date < date.today():
            raise ValueError("departure_date cannot be earlier than today")
        if self.return_date is not None and self.return_date < self.departure_date:
            raise ValueError("return_date cannot be earlier than departure_date")
        return self


class FlightSearchInput(TransportSearchInput):
    seat_classes: tuple[FlightSeatClass, ...] = ()
    flight_numbers: tuple[NonEmptyString, ...] = ()


class TrainSearchInput(TransportSearchInput):
    seat_classes: tuple[TrainSeatClass, ...] = ()
    train_numbers: tuple[NonEmptyString, ...] = ()


class HotelSearchInput(TravelSearchInput):
    destination: NonEmptyString = Field(
        description="Destination country, province, city, or district."
    )
    check_in_date: date = Field(description="Check-in date in YYYY-MM-DD format.")
    check_out_date: date = Field(description="Check-out date in YYYY-MM-DD format.")
    keywords: NonEmptyString | None = None
    nearby_poi: NonEmptyString | None = None
    hotel_types: tuple[HotelType, ...] = ()
    sort: HotelSort | None = None
    hotel_stars: tuple[Annotated[int, Field(ge=1, le=5)], ...] = ()
    bed_types: tuple[HotelBedType, ...] = ()
    max_price: PositiveNumber | None = Field(
        default=None,
        description="Maximum nightly price in CNY.",
    )

    @model_validator(mode="after")
    def validate_stay_dates(self) -> Self:
        if self.check_in_date < date.today():
            raise ValueError("check_in_date cannot be earlier than today")
        if self.check_in_date >= self.check_out_date:
            raise ValueError("check_in_date must be earlier than check_out_date")
        return self


class PoiSearchInput(TravelSearchInput):
    city: NonEmptyString = Field(description="City containing the attraction.")
    keyword: NonEmptyString = Field(description="Attraction name or search keyword.")
    poi_level: Annotated[int, Field(ge=1, le=5)] | None = None
    category: PoiCategory | None = None


class FlyAIErrorCode(StrEnum):
    CLI_NOT_FOUND = "CLI_NOT_FOUND"
    CLI_TIMEOUT = "CLI_TIMEOUT"
    CLI_EXIT_ERROR = "CLI_EXIT_ERROR"
    INVALID_JSON = "INVALID_JSON"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    REMOTE_SERVICE_ERROR = "REMOTE_SERVICE_ERROR"
    EMPTY_RESULT = "EMPTY_RESULT"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class FlyAIExecutionDiagnostics(BaseModel):
    """Independent process, provider, parse, and business-availability verdicts."""

    model_config = ConfigDict(extra="forbid")

    process_status: Literal["not_started", "success", "failed", "timeout"] = "not_started"
    process_return_code: int | None = None
    provider_status: Literal["unknown", "success", "failed"] = "unknown"
    parse_status: Literal["not_attempted", "success", "invalid", "empty"] = "not_attempted"
    business_status: Literal["unknown", "usable", "empty", "invalid"] = "unknown"


class FlyAIResult(BaseModel):
    """Provider-independent envelope returned to tools and future graph nodes."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    provider: Literal["flyai"] = "flyai"
    command: list[str]
    data: Any | None = None
    error_code: FlyAIErrorCode | None = None
    error_message: str | None = None
    duration_ms: int = Field(ge=0)
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diagnostics: FlyAIExecutionDiagnostics = Field(
        default_factory=FlyAIExecutionDiagnostics
    )

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.success and (self.error_code is not None or self.error_message is not None):
            raise ValueError("successful results cannot contain error details")
        if not self.success:
            if self.data is not None:
                raise ValueError("failed results cannot contain data")
            if self.error_code is None or not self.error_message:
                raise ValueError("failed results require an error code and message")
        return self
