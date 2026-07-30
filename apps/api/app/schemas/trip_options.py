from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.trip_capabilities import TransportMode
from app.schemas.trip_planning import TripPlanningModel


class TransportOptionSnapshot(TripPlanningModel):
    kind: Literal["transport"] = "transport"
    option_id: str = Field(min_length=1, max_length=100)
    provider: Literal["flyai"] = "flyai"
    mode: TransportMode
    direction: Literal["outbound", "return"]
    journey_type: str | None = None
    transport_names: list[str] = Field(default_factory=list)
    transport_numbers: list[str] = Field(default_factory=list)
    departure_station: str
    departure_at: str
    arrival_station: str
    arrival_at: str
    duration_minutes: int | None = Field(default=None, ge=1)
    seat_classes: list[str] = Field(default_factory=list)
    price_amount: Decimal | None = Field(default=None, ge=0)
    currency: Literal["CNY"] | None = None
    detail_url: str | None = None
    display_text: str


class HotelOptionSnapshot(TripPlanningModel):
    kind: Literal["hotel"] = "hotel"
    option_id: str = Field(min_length=1, max_length=100)
    provider: Literal["flyai"] = "flyai"
    provider_hotel_id: str | None = None
    name: str
    star: str | None = None
    price_amount: Decimal | None = Field(default=None, ge=0)
    currency: Literal["CNY"] | None = None
    nearby_poi: str | None = None
    address: str | None = None
    detail_url: str | None = None
    display_text: str


TripOptionSnapshot = Annotated[
    TransportOptionSnapshot | HotelOptionSnapshot,
    Field(discriminator="kind"),
]


__all__ = [
    "HotelOptionSnapshot",
    "TransportOptionSnapshot",
    "TripOptionSnapshot",
]
