from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import date
from ipaddress import IPv4Address, ip_address
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.clients.amap_cache import AmapCache, InMemoryAmapCache, ttl_for_namespace
from app.clients.amap_errors import (
    AmapConfigurationError,
    AmapEmptyResultError,
    AmapError,
    AmapInvalidParameterError,
    AmapRateLimitError,
    AmapRequestError,
    AmapTimeoutError,
)
from app.schemas.amap import (
    AmapCoordinate,
    AmapCoordinateInput,
    AmapPlace,
    ConvertCoordinateInput,
    CoordinateSystem,
    CurrentCityResult,
    CurrentWeather,
    GeocodeResult,
    MatrixEntry,
    MatrixLocation,
    MatrixMode,
    PlaceSearchResult,
    ReverseGeocodeResult,
    RouteMode,
    RoutePlanInput,
    RouteResult,
    RouteStep,
    SearchPlacesInput,
    TravelTimeMatrixInput,
    TravelTimeMatrixResult,
    WeatherForecast,
    WeatherInput,
    WeatherResult,
)

logger = logging.getLogger(__name__)

_ROUTE_ENDPOINTS = {
    RouteMode.WALKING: "/v5/direction/walking",
    RouteMode.DRIVING: "/v5/direction/driving",
    RouteMode.TRANSIT: "/v5/direction/transit/integrated",
    RouteMode.BICYCLING: "/v5/direction/bicycling",
    RouteMode.ELECTRIC_BIKE: "/v5/direction/electrobike",
}
_COORDINATE_SYSTEMS = {
    CoordinateSystem.WGS84: "gps",
    CoordinateSystem.BD09: "baidu",
    CoordinateSystem.MAPBAR: "mapbar",
    CoordinateSystem.GCJ02: "autonavi",
}
_RATE_LIMIT_INFOCODES = frozenset({"10003", "10004", "10010", "10019", "10020", "10021", "10044"})
_CONFIGURATION_INFOCODES = frozenset(
    {"10001", "10005", "10006", "10007", "10008", "10009", "10011", "10012"}
)


class AmapClient:
    """Typed async client for the Amap Web Service API."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://restapi.amap.com",
        timeout_seconds: float = 15,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.25,
        min_request_interval_seconds: float = 0.2,
        matrix_batch_size: int = 100,
        cache: AmapCache | None = None,
        cache_ttl_overrides: Mapping[str, float] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized_key = (api_key or "").strip()
        if not normalized_key:
            raise AmapConfigurationError("AMAP_API_KEY is not configured.")
        parsed_base_url = urlsplit(base_url)
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.netloc
            or parsed_base_url.username
            or parsed_base_url.password
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise AmapConfigurationError("AMAP_BASE_URL must be a plain HTTP(S) origin.")
        if timeout_seconds <= 0:
            raise AmapConfigurationError("AMAP_TIMEOUT_SECONDS must be positive.")
        if max_retries < 0 or max_retries > 5:
            raise AmapConfigurationError("AMAP_MAX_RETRIES must be between 0 and 5.")
        if retry_delay_seconds < 0:
            raise AmapConfigurationError("Amap retry delay cannot be negative.")
        if min_request_interval_seconds < 0:
            raise AmapConfigurationError("Amap request interval cannot be negative.")
        if not 1 <= matrix_batch_size <= 100:
            raise AmapConfigurationError("Amap matrix batch size must be between 1 and 100.")

        self._api_key = normalized_key
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._min_request_interval_seconds = min_request_interval_seconds
        self._request_slot_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._sleep = asyncio.sleep
        self._matrix_batch_size = matrix_batch_size
        self._cache = cache or InMemoryAmapCache()
        self._cache_ttl_overrides = dict(cache_ttl_overrides or {})
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds),
            follow_redirects=False,
        )

        # HTTPX logs full query strings at INFO, which would reveal Amap's query-string key.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    async def ip_location(self, client_ip: str) -> CurrentCityResult:
        try:
            parsed_ip = ip_address(client_ip)
        except ValueError:
            raise AmapInvalidParameterError("Client IP is not a valid IP address.") from None
        if not isinstance(parsed_ip, IPv4Address):
            raise AmapInvalidParameterError("Amap IP location supports IPv4 only.")

        payload = await self._request("/v3/ip", {"ip": str(parsed_ip)})
        province = self._text(payload.get("province")) or None
        city = self._text(payload.get("city")) or None
        adcode = self._text(payload.get("adcode")) or None
        locatable = bool(city and adcode)
        return CurrentCityResult(
            province=province,
            city=city,
            adcode=adcode,
            accuracy_level="city" if locatable else "unavailable",
            locatable=locatable,
            unavailable_reason=None if locatable else "Amap could not resolve this IP to a city.",
        )

    async def search_places(self, query: SearchPlacesInput) -> PlaceSearchResult:
        params: dict[str, object] = {
            "keywords": query.keywords,
            "types": query.poi_type,
            "city": query.adcode or query.city,
            "citylimit": "true" if query.adcode or query.city else None,
            "offset": query.limit,
            "page": 1,
            "extensions": "base",
        }
        if query.location is None:
            endpoint = "/v3/place/text"
        else:
            endpoint = "/v3/place/around"
            params.update(
                {
                    "location": self._coordinate(query.location),
                    "radius": query.radius_meters,
                    "sortrule": "distance",
                }
            )

        payload = await self._request(
            endpoint,
            params,
            cache_namespace="place_search_v2",
        )
        pois = [
            place
            for item in self._list(payload.get("pois"))[: query.limit]
            if (place := self._parse_place(item)) is not None
        ]
        return PlaceSearchResult(pois=pois)

    async def resolve_location(
        self,
        name: str,
        *,
        city: str | None = None,
        adcode: str | None = None,
    ) -> AmapPlace:
        result = await self.search_places(
            SearchPlacesInput(keywords=name, city=city, adcode=adcode, limit=5)
        )
        if not result.pois:
            raise AmapEmptyResultError("Amap returned no matching place.")
        if len(result.pois) != 1:
            raise AmapInvalidParameterError(
                "The place name is ambiguous; provide a city, adcode, or coordinates."
            )
        return result.pois[0]

    async def geocode(self, address: str, *, city: str | None = None) -> list[GeocodeResult]:
        normalized = address.strip()
        if not normalized:
            raise AmapInvalidParameterError("Geocode address cannot be empty.")
        payload = await self._request(
            "/v3/geocode/geo",
            {"address": normalized, "city": city},
            cache_namespace="geocode",
        )
        results: list[GeocodeResult] = []
        for item in self._list(payload.get("geocodes")):
            location = self._parse_coordinate(item.get("location"))
            if location is None:
                continue
            results.append(
                GeocodeResult(
                    formatted_address=self._text(item.get("formatted_address")),
                    province=self._text(item.get("province")),
                    city=self._text(item.get("city")),
                    district=self._text(item.get("district")),
                    adcode=self._text(item.get("adcode")),
                    citycode=self._text(item.get("citycode")),
                    location=location,
                )
            )
        return results

    async def reverse_geocode(
        self,
        location: AmapCoordinateInput,
        *,
        radius_meters: int = 1000,
    ) -> ReverseGeocodeResult:
        if not 0 <= radius_meters <= 3000:
            raise AmapInvalidParameterError(
                "Reverse-geocode radius must be between 0 and 3000 meters."
            )
        payload = await self._request(
            "/v3/geocode/regeo",
            {
                "location": self._coordinate(location),
                "radius": radius_meters,
                "extensions": "all",
            },
            cache_namespace="reverse_geocode",
        )
        regeocode = self._dict(payload.get("regeocode"))
        if not regeocode:
            raise AmapEmptyResultError("Amap returned no reverse-geocode result.")
        component = self._dict(regeocode.get("addressComponent"))
        nearby_pois = [
            place
            for item in self._list(regeocode.get("pois"))
            if (place := self._parse_place(item)) is not None
        ]
        return ReverseGeocodeResult(
            formatted_address=self._text(regeocode.get("formatted_address")),
            province=self._text(component.get("province")),
            city=self._text(component.get("city")),
            district=self._text(component.get("district")),
            adcode=self._text(component.get("adcode")),
            nearby_pois=nearby_pois,
        )

    async def convert_coordinates(self, query: ConvertCoordinateInput) -> AmapCoordinate:
        payload = await self._request(
            "/v3/assistant/coordinate/convert",
            {
                "locations": self._coordinate(query),
                "coordsys": _COORDINATE_SYSTEMS[query.source_coordinate_system],
            },
            cache_namespace="coordinate_conversion",
        )
        converted = self._parse_coordinate(self._text(payload.get("locations")).split(";")[0])
        if converted is None:
            raise AmapEmptyResultError("Amap returned no converted coordinate.")
        return converted.model_copy(update={"source": "amap_conversion"})

    async def plan_route(self, query: RoutePlanInput) -> RouteResult:
        params: dict[str, object] = {
            "origin": self._coordinate(query.origin),
            "destination": self._coordinate(query.destination),
            "strategy": query.strategy,
            "show_fields": "cost,navi,polyline",
        }
        if query.mode == RouteMode.DRIVING and query.waypoints:
            params["waypoints"] = ";".join(self._coordinate(point) for point in query.waypoints)
        if query.mode == RouteMode.TRANSIT:
            assert query.city is not None
            city1 = await self._resolve_citycode(query.city)
            city2 = await self._resolve_citycode(query.destination_city or query.city)
            params.update({"city1": city1, "city2": city2})

        payload = await self._request(
            _ROUTE_ENDPOINTS[query.mode],
            params,
            cache_namespace="route_plan",
        )
        route = self._dict(payload.get("route"))
        if query.mode == RouteMode.TRANSIT:
            return self._parse_transit_route(query.mode, route)
        return self._parse_standard_route(query.mode, route)

    async def travel_time_matrix(
        self,
        query: TravelTimeMatrixInput,
    ) -> TravelTimeMatrixResult:
        locations = self._deduplicate_locations(query.locations)
        if len(locations) < 2:
            raise AmapInvalidParameterError(
                "At least two distinct coordinates are required for a matrix."
            )

        entries: list[MatrixEntry] = []
        for destination in locations:
            origins = [item for item in locations if item.id != destination.id]
            for batch in self._batches(origins, self._matrix_batch_size):
                payload = await self._request(
                    "/v3/distance",
                    {
                        "origins": "|".join(self._matrix_coordinate(item) for item in batch),
                        "destination": self._matrix_coordinate(destination),
                        "type": 1 if query.mode == MatrixMode.DRIVING else 3,
                    },
                    cache_namespace="travel_time_matrix",
                )
                raw_results = self._list(payload.get("results"))
                for index, origin in enumerate(batch):
                    item = raw_results[index] if index < len(raw_results) else {}
                    item_info = self._text(item.get("info"))
                    distance = self._number(item.get("distance"))
                    duration = self._number(item.get("duration"))
                    if item_info and item_info.casefold() not in {"ok", "success"}:
                        entries.append(
                            MatrixEntry(
                                origin_id=origin.id,
                                destination_id=destination.id,
                                success=False,
                                error_code=self._text(item.get("code")) or None,
                                error_message="Amap could not calculate this matrix pair.",
                            )
                        )
                    elif distance is None or duration is None:
                        entries.append(
                            MatrixEntry(
                                origin_id=origin.id,
                                destination_id=destination.id,
                                success=False,
                                error_message="Amap returned an incomplete matrix pair.",
                            )
                        )
                    else:
                        entries.append(
                            MatrixEntry(
                                origin_id=origin.id,
                                destination_id=destination.id,
                                success=True,
                                distance_meters=distance,
                                duration_seconds=duration,
                            )
                        )
        return TravelTimeMatrixResult(mode=query.mode, locations=locations, matrix=entries)

    async def get_weather(self, query: WeatherInput) -> WeatherResult:
        adcode = query.adcode or await self._resolve_adcode(query.city or "")
        current_payload = await self._request(
            "/v3/weather/weatherInfo",
            {"city": adcode, "extensions": "base"},
            cache_namespace="weather_current",
        )
        forecast_payload: dict[str, Any] | None = None
        if query.forecast:
            forecast_payload = await self._request(
                "/v3/weather/weatherInfo",
                {"city": adcode, "extensions": "all"},
                cache_namespace="weather_forecast",
            )

        lives = self._list(current_payload.get("lives"))
        if not lives:
            raise AmapEmptyResultError("Amap returned no current weather for this city.")
        live = lives[0]
        current = CurrentWeather(
            weather=self._text(live.get("weather")),
            temperature=self._text(live.get("temperature")),
            humidity=self._text(live.get("humidity")),
            wind_direction=self._text(live.get("winddirection")),
            wind_power=self._text(live.get("windpower")),
            report_time=self._text(live.get("reporttime")),
        )

        forecasts: list[WeatherForecast] = []
        if forecast_payload is not None:
            forecast_groups = self._list(forecast_payload.get("forecasts"))
            if forecast_groups:
                for item in self._list(forecast_groups[0].get("casts")):
                    forecast_date = self._date(item.get("date"))
                    if forecast_date is None:
                        continue
                    forecasts.append(
                        WeatherForecast(
                            date=forecast_date,
                            day_weather=self._text(item.get("dayweather")),
                            night_weather=self._text(item.get("nightweather")),
                            day_temperature=self._text(item.get("daytemp")),
                            night_temperature=self._text(item.get("nighttemp")),
                            day_wind_direction=self._text(item.get("daywind")),
                            night_wind_direction=self._text(item.get("nightwind")),
                            day_wind_power=self._text(item.get("daypower")),
                            night_wind_power=self._text(item.get("nightpower")),
                        )
                    )
        return WeatherResult(
            city=self._text(live.get("city")),
            adcode=self._text(live.get("adcode")) or adcode,
            province=self._text(live.get("province")),
            current=current,
            forecast=forecasts,
        )

    async def _resolve_adcode(self, city: str) -> str:
        candidates = await self.geocode(city, city=city)
        adcodes = {item.adcode for item in candidates if item.adcode}
        if not adcodes:
            raise AmapEmptyResultError("Amap could not resolve the city to an adcode.")
        if len(adcodes) != 1:
            raise AmapInvalidParameterError(
                "The city name is ambiguous; provide a six-digit adcode."
            )
        return adcodes.pop()

    async def _resolve_citycode(self, city: str) -> str:
        normalized = city.strip()
        if normalized.isascii() and normalized.isdigit() and 3 <= len(normalized) <= 4:
            return normalized
        candidates = await self.geocode(normalized, city=normalized)
        citycodes = {item.citycode for item in candidates if item.citycode}
        if not citycodes:
            raise AmapEmptyResultError("Amap could not resolve the transit city code.")
        if len(citycodes) != 1:
            raise AmapInvalidParameterError(
                "The transit city is ambiguous; provide an Amap citycode."
            )
        return citycodes.pop()

    async def _request(
        self,
        endpoint: str,
        params: Mapping[str, object | None],
        *,
        cache_namespace: str | None = None,
    ) -> dict[str, Any]:
        safe_params = {key: value for key, value in params.items() if value is not None}
        cache_key = self._cache_key(cache_namespace, endpoint, safe_params)
        if cache_key is not None and (cached := await self._cache.get(cache_key)) is not None:
            logger.info("Amap request cache hit endpoint=%s", endpoint)
            return cached

        attempts = self._max_retries + 1
        for attempt in range(attempts):
            started = perf_counter()
            try:
                await self._wait_for_request_slot()
                response = await self._http.get(
                    endpoint,
                    params={**safe_params, "key": self._api_key, "output": "JSON"},
                )
            except httpx.TimeoutException:
                logger.warning("Amap request timed out endpoint=%s", endpoint)
                if attempt + 1 < attempts:
                    await self._retry_delay(attempt)
                    continue
                raise AmapTimeoutError("The Amap request timed out.") from None
            except httpx.RequestError:
                logger.warning("Amap network request failed endpoint=%s", endpoint)
                if attempt + 1 < attempts:
                    await self._retry_delay(attempt)
                    continue
                raise AmapRequestError("The Amap service could not be reached.") from None

            duration_ms = round((perf_counter() - started) * 1000)
            if response.status_code == 429:
                logger.warning(
                    "Amap HTTP rate limit endpoint=%s duration_ms=%d",
                    endpoint,
                    duration_ms,
                )
                if attempt + 1 < attempts:
                    await self._retry_delay(
                        attempt,
                        retry_after_seconds=self._retry_after_seconds(response),
                    )
                    continue
                raise AmapRateLimitError("The Amap service rate limit was reached.")
            if response.status_code >= 500:
                logger.warning(
                    "Amap temporary HTTP failure endpoint=%s status=%d duration_ms=%d",
                    endpoint,
                    response.status_code,
                    duration_ms,
                )
                if attempt + 1 < attempts:
                    await self._retry_delay(attempt)
                    continue
                raise AmapRequestError("The Amap service returned a temporary HTTP error.")
            if response.status_code in {401, 403}:
                raise AmapConfigurationError(
                    "Amap rejected the configured API credentials or permissions."
                )
            if response.status_code >= 400:
                raise AmapInvalidParameterError("Amap rejected the HTTP request parameters.")

            try:
                payload = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                raise AmapRequestError("Amap returned an invalid JSON response.") from None
            if not isinstance(payload, dict):
                raise AmapRequestError("Amap returned an unexpected response structure.")

            infocode = self._text(payload.get("infocode")) or None
            if self._text(payload.get("status")) != "1":
                error = self._business_error(infocode)
                logger.warning(
                    "Amap business failure endpoint=%s infocode=%s duration_ms=%d",
                    endpoint,
                    infocode or "unknown",
                    duration_ms,
                )
                if isinstance(error, AmapRateLimitError) and attempt + 1 < attempts:
                    await self._retry_delay(attempt)
                    continue
                raise error

            logger.info(
                "Amap request completed endpoint=%s infocode=%s duration_ms=%d",
                endpoint,
                infocode or "10000",
                duration_ms,
            )
            if cache_key is not None and cache_namespace is not None:
                await self._cache.set(
                    cache_key,
                    payload,
                    ttl_seconds=ttl_for_namespace(cache_namespace, self._cache_ttl_overrides),
                )
            return payload

        raise AssertionError("Amap request retry loop exited unexpectedly")

    async def _wait_for_request_slot(self) -> None:
        if self._min_request_interval_seconds <= 0:
            return
        async with self._request_slot_lock:
            delay = max(0.0, self._next_request_at - monotonic())
            if delay:
                await self._sleep(delay)
            self._next_request_at = monotonic() + self._min_request_interval_seconds

    async def _retry_delay(
        self,
        attempt: int,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        exponential_delay = self._retry_delay_seconds * (2**attempt)
        delay = max(exponential_delay, retry_after_seconds or 0.0)
        if delay:
            await self._sleep(delay)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _business_error(infocode: str | None) -> AmapError:
        if infocode in _RATE_LIMIT_INFOCODES:
            return AmapRateLimitError("The Amap service rate limit was reached.", infocode=infocode)
        if infocode in _CONFIGURATION_INFOCODES:
            return AmapConfigurationError(
                "Amap rejected the configured API credentials or permissions.",
                infocode=infocode,
            )
        if infocode and infocode.startswith("2"):
            return AmapInvalidParameterError(
                "Amap rejected one or more request parameters.", infocode=infocode
            )
        return AmapError("The Amap service rejected the request.", infocode=infocode)

    @staticmethod
    def _cache_key(
        namespace: str | None,
        endpoint: str,
        params: Mapping[str, object],
    ) -> str | None:
        if namespace is None:
            return None
        canonical = json.dumps(
            {"endpoint": endpoint, "params": params},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def _parse_standard_route(self, mode: RouteMode, route: dict[str, Any]) -> RouteResult:
        paths = self._list(route.get("paths"))
        if not paths:
            raise AmapEmptyResultError("Amap returned no route for these coordinates.")
        path = paths[0]
        steps = [self._parse_standard_step(item, mode) for item in self._list(path.get("steps"))]
        distance = self._number(path.get("distance"))
        cost = self._dict(path.get("cost"))
        duration = self._number(cost.get("duration")) or self._number(path.get("duration"))
        if distance is None or duration is None:
            raise AmapEmptyResultError("Amap returned an incomplete route result.")
        instructions = [step.instruction for step in steps if step.instruction]
        polyline_parts = [step.polyline for step in steps if step.polyline]
        return RouteResult(
            mode=mode,
            distance_meters=distance,
            duration_seconds=duration,
            route_summary="; ".join(instructions[:8]) or f"Amap {mode.value} route",
            steps=steps,
            polyline=";".join(polyline_parts) if polyline_parts else None,
        )

    def _parse_standard_step(self, item: dict[str, Any], mode: RouteMode) -> RouteStep:
        cost = self._dict(item.get("cost"))
        return RouteStep(
            instruction=self._text(item.get("instruction")),
            transport=mode.value,
            road=self._text(item.get("road_name")) or self._text(item.get("road")),
            distance_meters=self._number(item.get("step_distance"))
            or self._number(item.get("distance")),
            duration_seconds=self._number(cost.get("duration"))
            or self._number(item.get("duration")),
            polyline=self._text(item.get("polyline")) or None,
        )

    def _parse_transit_route(self, mode: RouteMode, route: dict[str, Any]) -> RouteResult:
        transits = self._list(route.get("transits"))
        if not transits:
            raise AmapEmptyResultError("Amap returned no public-transit route.")
        transit = transits[0]
        cost = self._dict(transit.get("cost"))
        distance = self._number(transit.get("distance"))
        duration = self._number(cost.get("duration")) or self._number(transit.get("duration"))
        if distance is None or duration is None:
            raise AmapEmptyResultError("Amap returned an incomplete public-transit route.")

        steps: list[RouteStep] = []
        walking_distance = 0
        bus_legs = 0
        for segment in self._list(transit.get("segments")):
            walking = self._dict(segment.get("walking"))
            walking_distance += self._number(walking.get("distance")) or 0
            for item in self._list(walking.get("steps")):
                steps.append(self._parse_standard_step(item, RouteMode.WALKING))

            bus = self._dict(segment.get("bus"))
            buslines = self._list(bus.get("buslines")) or self._list(bus.get("steps"))
            for line in buslines:
                name = self._text(line.get("name")) or "Public transit"
                steps.append(
                    RouteStep(
                        instruction=f"Take {name}",
                        transport="public_transit",
                        distance_meters=self._number(line.get("distance")),
                        duration_seconds=self._number(line.get("duration")),
                        polyline=self._text(line.get("polyline")) or None,
                    )
                )
                bus_legs += 1

            railway = self._dict(segment.get("railway"))
            if railway:
                steps.append(
                    RouteStep(
                        instruction=f"Take {self._text(railway.get('name')) or 'railway'}",
                        transport="railway",
                        distance_meters=self._number(railway.get("distance")),
                        duration_seconds=self._number(railway.get("time")),
                    )
                )
                bus_legs += 1

        instructions = [step.instruction for step in steps if step.instruction]
        polyline_parts = [step.polyline for step in steps if step.polyline]
        return RouteResult(
            mode=mode,
            distance_meters=distance,
            duration_seconds=duration,
            route_summary="; ".join(instructions[:8]) or "Amap public-transit route",
            steps=steps,
            transfers=max(0, bus_legs - 1),
            walking_distance_meters=walking_distance,
            taxi_cost=self._decimal(cost.get("taxi_fee")),
            polyline=";".join(polyline_parts) if polyline_parts else None,
        )

    @staticmethod
    def _deduplicate_locations(locations: Sequence[MatrixLocation]) -> list[MatrixLocation]:
        deduplicated: list[MatrixLocation] = []
        seen: set[tuple[float, float]] = set()
        for item in locations:
            key = (round(item.longitude, 6), round(item.latitude, 6))
            if key not in seen:
                seen.add(key)
                deduplicated.append(item)
        return deduplicated

    @staticmethod
    def _batches(
        values: Sequence[MatrixLocation],
        batch_size: int,
    ) -> list[Sequence[MatrixLocation]]:
        return [values[index : index + batch_size] for index in range(0, len(values), batch_size)]

    @staticmethod
    def _coordinate(value: Any) -> str:
        return f"{value.longitude:.6f},{value.latitude:.6f}"

    @staticmethod
    def _matrix_coordinate(value: MatrixLocation) -> str:
        return f"{value.longitude:.6f},{value.latitude:.6f}"

    @classmethod
    def _parse_coordinate(cls, value: object) -> AmapCoordinate | None:
        rendered = cls._text(value)
        parts = rendered.split(",")
        if len(parts) != 2:
            return None
        try:
            return AmapCoordinate(longitude=float(parts[0]), latitude=float(parts[1]))
        except ValueError:
            return None

    @classmethod
    def _parse_place(cls, item: dict[str, Any]) -> AmapPlace | None:
        location = cls._parse_coordinate(item.get("location"))
        if location is None:
            return None
        return AmapPlace(
            poi_id=cls._text(item.get("id")),
            name=cls._text(item.get("name")),
            address=cls._text(item.get("address")),
            province=cls._text(item.get("pname")),
            city=cls._text(item.get("cityname")),
            district=cls._text(item.get("adname")),
            adcode=cls._text(item.get("adcode")),
            poi_type=cls._text(item.get("type")) or cls._text(item.get("typecode")),
            distance_meters=cls._number(item.get("distance")),
            location=location,
        )

    @staticmethod
    def _dict(value: object) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _list(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _text(value: object) -> str:
        if value is None or isinstance(value, (list, dict)):
            return ""
        return str(value).strip()

    @classmethod
    def _number(cls, value: object) -> int | None:
        rendered = cls._text(value)
        if not rendered:
            return None
        try:
            number = int(float(rendered))
        except ValueError:
            return None
        return max(0, number)

    @classmethod
    def _decimal(cls, value: object) -> float | None:
        rendered = cls._text(value)
        if not rendered:
            return None
        try:
            return max(0, float(rendered))
        except ValueError:
            return None

    @classmethod
    def _date(cls, value: object) -> date | None:
        try:
            return date.fromisoformat(cls._text(value))
        except ValueError:
            return None


__all__ = ["AmapClient"]
