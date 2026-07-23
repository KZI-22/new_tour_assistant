from __future__ import annotations

from app.schemas.trip_capabilities import CapabilityPlan, TripPlanningRequest
from app.schemas.trip_evidence import (
    EvidenceStatus,
    JoinedTripEvidence,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)


def join_trip_evidence(
    request: TripPlanningRequest,
    capabilities: CapabilityPlan,
    map_weather: MapWeatherEvidenceBundle,
    transport: RawCapabilityEvidence,
    hotel: RawCapabilityEvidence,
) -> JoinedTripEvidence:
    warnings = list(
        dict.fromkeys(
            [
                *map_weather.warnings,
                *transport.warnings,
                *hotel.warnings,
            ]
        )
    )
    if map_weather.status == "failed" or map_weather.map is None or map_weather.weather is None:
        overall_status = "failed"
    elif map_weather.status == "partial" or _optional_is_partial(
        capabilities,
        transport,
        hotel,
    ):
        overall_status = "partial"
    else:
        overall_status = "usable"
    return JoinedTripEvidence(
        request=request,
        capabilities=capabilities,
        map_weather=map_weather,
        transport=transport,
        hotel=hotel,
        overall_status=overall_status,
        warnings=warnings,
    )


def _optional_is_partial(
    capabilities: CapabilityPlan,
    transport: RawCapabilityEvidence,
    hotel: RawCapabilityEvidence,
) -> bool:
    expected = (
        (capabilities.transport.enabled, transport),
        (capabilities.hotel.enabled, hotel),
    )
    return any(
        enabled and evidence.status is not EvidenceStatus.USABLE for enabled, evidence in expected
    )


__all__ = ["join_trip_evidence"]
