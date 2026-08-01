from __future__ import annotations

from datetime import UTC, date, datetime

from app.schemas.map_planning import ExcludedAttractionEvidence, MapTripEvidence
from app.schemas.trip_capabilities import CapabilityPlan, TripPlanningRequest
from app.schemas.trip_evidence import (
    EvidenceStatus,
    JoinedTripEvidence,
    MapWeatherEvidenceBundle,
    RawCapabilityEvidence,
)
from app.schemas.trip_planning import CityTripRequest, TripWeatherEvidence
from app.services.trip_plan_snapshot_builder import build_trip_plan_snapshot


def test_snapshot_persists_attraction_exclusion_diagnostics() -> None:
    queried_at = datetime(2026, 8, 1, tzinfo=UTC)
    map_evidence = MapTripEvidence(
        city="北京",
        planning_run_id="run-1",
        queried_at=queried_at,
        days=[],
        excluded_attractions=[
            ExcludedAttractionEvidence(
                poi_id="company-1",
                name="联动文化(北京)有限公司",
                poi_type="公司企业;公司;公司",
                stage="provider_filter",
                reason="POI 类型属于企业、园区或商务住宅，非游览景点",
                source_queries=["城市地标"],
                best_search_rank=1,
            )
        ],
    )
    evidence = JoinedTripEvidence(
        request=TripPlanningRequest(
            core=CityTripRequest(
                destination_city="北京",
                duration_days=1,
                start_date=date(2026, 8, 2),
            )
        ),
        capabilities=CapabilityPlan(),
        map_weather=MapWeatherEvidenceBundle(
            status="usable",
            map=map_evidence,
            weather=TripWeatherEvidence(city="北京", queried_at=queried_at, days=[]),
        ),
        transport=RawCapabilityEvidence(
            capability="transport",
            status=EvidenceStatus.SKIPPED,
            query={"enabled": False},
            queried_at=queried_at,
            duration_ms=0,
        ),
        hotel=RawCapabilityEvidence(
            capability="hotel",
            status=EvidenceStatus.SKIPPED,
            query={"enabled": False},
            queried_at=queried_at,
            duration_ms=0,
        ),
        overall_status="usable",
    )

    snapshot = build_trip_plan_snapshot(evidence)

    assert [item.model_dump() for item in snapshot.source_metadata.attraction_exclusions] == [
        {
            "provider_place_id": "company-1",
            "name": "联动文化(北京)有限公司",
            "poi_type": "公司企业;公司;公司",
            "stage": "provider_filter",
            "reason": "POI 类型属于企业、园区或商务住宅，非游览景点",
            "source_queries": ["城市地标"],
            "best_search_rank": 1,
            "candidate_score": None,
        }
    ]
