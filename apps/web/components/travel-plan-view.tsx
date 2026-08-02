"use client";

import {
  ArrowLeft,
  BedDouble,
  BusFront,
  CalendarDays,
  Check,
  ChevronRight,
  CircleAlert,
  Clock3,
  CloudSun,
  ExternalLink,
  Utensils,
  Hotel,
  LoaderCircle,
  MapPinned,
  Navigation,
  Plane,
  Route,
  Share2,
  Sparkles,
  TrainFront,
  TriangleAlert,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AssistantWidget } from "@/components/assistant-widget";
import { TravelSearchDialog, type SearchKind } from "@/components/travel-search-dialog";
import {
  fetchTravelPlan,
  type HotelOption,
  type RestaurantRecommendation,
  type TransportOption,
  type TravelPlanDetail,
  type TravelPlanSnapshotV1,
  type TripPlanDay,
  type TripPlanPlace,
  type TripPlanRouteLeg,
} from "@/lib/api";

type AmapMapInstance = {
  add: (overlays: object[]) => void;
  setFitView: (overlays?: object[], immediately?: boolean, padding?: number[]) => void;
  on: (event: "complete", handler: () => void) => void;
  destroy: () => void;
};

type AmapMarkerInstance = {
  on: (event: "click", handler: () => void) => void;
  setContent: (content: string) => void;
};

type AmapRouteResult = {
  routes?: Array<{
    distance?: number | string;
    time?: number | string;
  }>;
  info?: string;
};

type AmapRouteService = {
  clear: () => void;
  search: (
    origin: [number, number],
    destination: [number, number],
    callback: (status: string, result: AmapRouteResult | string) => void,
  ) => void;
};

type AmapRouteServiceConstructor = new (options: {
  map: AmapMapInstance;
  hideMarkers: boolean;
  showTraffic?: boolean;
}) => AmapRouteService;

type AmapServiceResult = {
  info?: string;
  infocode?: string | number;
};

type AmapDistrictSearchService = {
  search: (
    keyword: string,
    callback: (status: string, result: AmapServiceResult | string) => void,
  ) => void;
};

type AmapApi = {
  Map: new (
    container: HTMLElement,
    options: { zoom: number; viewMode: "2D"; mapStyle: string; resizeEnable: boolean },
  ) => AmapMapInstance;
  Marker: new (options: {
    position: [number, number];
    anchor: "center";
    content: string;
    title: string;
  }) => AmapMarkerInstance;
  Polyline: new (options: {
    path: Array<[number, number]>;
    strokeColor: string;
    strokeWeight: number;
    strokeOpacity: number;
    strokeStyle: "dashed";
    lineJoin: "round";
  }) => object;
  plugin: (plugins: string | string[], callback: () => void) => void;
  Walking?: AmapRouteServiceConstructor;
  Driving?: AmapRouteServiceConstructor;
  DistrictSearch?: new (options: { subdistrict: number; extensions: "base" }) => AmapDistrictSearchService;
};

declare global {
  interface Window {
    AMap?: AmapApi;
    _AMapSecurityConfig?: { securityJsCode: string };
  }
}

let amapLoader: Promise<AmapApi> | null = null;
let amapValidation: { key: string; promise: Promise<void> } | null = null;

function loadAmap(key: string, securityCode?: string): Promise<AmapApi> {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (amapLoader) return amapLoader;
  if (securityCode) window._AMapSecurityConfig = { securityJsCode: securityCode };

  const existing = document.querySelector<HTMLScriptElement>("script[data-tour-amap]");
  existing?.remove();

  const script = document.createElement("script");
  const loading = new Promise<AmapApi>((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      reject(new Error("高德地图加载超时，请检查网络、Key 和域名白名单。"));
    }, 15_000);
    const finish = () => {
      window.clearTimeout(timeoutId);
      if (window.AMap) resolve(window.AMap);
      else reject(new Error("高德地图脚本未正确加载，请确认 Key 类型为 Web端（JS API）。"));
    };
    script.dataset.tourAmap = "true";
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}`;
    script.async = true;
    script.addEventListener("load", finish, { once: true });
    script.addEventListener(
      "error",
      () => {
        window.clearTimeout(timeoutId);
        reject(new Error("高德地图脚本加载失败，请检查网络连接。"));
      },
      { once: true },
    );
    document.head.appendChild(script);
  });
  amapLoader = loading.catch((reason: unknown) => {
    amapLoader = null;
    script.remove();
    throw reason;
  });
  return amapLoader;
}

function amapValidationError(result: AmapServiceResult | string): Error {
  if (typeof result === "string") return new Error("高德 JS API 鉴权失败，请检查 Key 配置。");
  const code = result.infocode === undefined ? null : String(result.infocode);
  if (code === "10009" || result.info === "USERKEY_PLAT_NOMATCH") {
    return new Error(
      "高德鉴权失败：NEXT_PUBLIC_AMAP_JS_KEY 不是“Web端（JS API）”Key（10009 USERKEY_PLAT_NOMATCH）。请在高德控制台新建对应平台的 Key，并填写它配套的安全密钥。",
    );
  }
  const providerDetail = [code, result.info].filter(Boolean).join(" / ");
  return new Error(
    providerDetail
      ? `高德 JS API 鉴权失败（${providerDetail}），请检查 Key、安全密钥和域名白名单。`
      : "高德 JS API 鉴权失败，请检查 Key、安全密钥和域名白名单。",
  );
}

function validateAmapKey(AMap: AmapApi, key: string): Promise<void> {
  if (amapValidation?.key === key) return amapValidation.promise;

  const validation = new Promise<void>((resolve, reject) => {
    const timeoutId = window.setTimeout(
      () => reject(new Error("高德 JS API 鉴权检查超时，请检查网络连接。")),
      10_000,
    );
    try {
      AMap.plugin("AMap.DistrictSearch", () => {
        try {
          const DistrictSearch = AMap.DistrictSearch;
          if (!DistrictSearch) throw new Error("高德行政区查询插件未加载。");
          const service = new DistrictSearch({ subdistrict: 0, extensions: "base" });
          service.search("北京市", (status, result) => {
            window.clearTimeout(timeoutId);
            if (status === "complete") resolve();
            else reject(amapValidationError(result));
          });
        } catch (reason) {
          window.clearTimeout(timeoutId);
          reject(reason);
        }
      });
    } catch (reason) {
      window.clearTimeout(timeoutId);
      reject(reason);
    }
  });
  const checked = validation.catch((reason: unknown) => {
    amapValidation = null;
    throw reason;
  });
  amapValidation = { key, promise: checked };
  return checked;
}

function formatDate(value: string | null): string {
  if (!value) return "日期待确认";
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(date);
}

function formatQueryTime(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(minutes: number | null): string | null {
  if (!minutes) return null;
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  if (hours && remaining) return `${hours}小时${remaining}分`;
  if (hours) return `${hours}小时`;
  return `${remaining}分钟`;
}

function formatDistance(meters: number | null): string | null {
  if (meters === null) return null;
  return meters >= 1000 ? `${(meters / 1000).toFixed(1)} 公里` : `${meters} 米`;
}

function formatPrice(value: string | number | null): string | null {
  if (value === null || value === "") return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return `¥${value}`;
  return `¥${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(amount)} 起`;
}

function safeDetailUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.toString() : null;
  } catch {
    return null;
  }
}

function amapPlaceUrl(place: Pick<TripPlanPlace, "location" | "name">): string {
  const position = `${place.location.longitude},${place.location.latitude}`;
  return `https://uri.amap.com/marker?position=${position}&name=${encodeURIComponent(place.name)}&src=tour-assistant&coordinate=gaode&callnative=1`;
}

type InteractiveRouteMode = "walking" | "driving";

function amapRouteUrl(
  origin: TripPlanPlace,
  destination: TripPlanPlace,
  mode: InteractiveRouteMode,
): string {
  const params = new URLSearchParams({
    from: `${origin.location.longitude},${origin.location.latitude},${origin.name}`,
    to: `${destination.location.longitude},${destination.location.latitude},${destination.name}`,
    mode: mode === "walking" ? "walk" : "car",
    src: "tour-assistant",
    coordinate: "gaode",
    callnative: "1",
  });
  return `https://uri.amap.com/navigation?${params.toString()}`;
}

function routeModeLabel(mode: TripPlanRouteLeg["mode"]): string {
  const labels: Record<TripPlanRouteLeg["mode"], string> = {
    walking: "步行",
    transit: "公共交通",
    driving: "驾车/打车",
    estimated: "距离估算",
    unverified: "路线待确认",
  };
  return labels[mode];
}

function weatherLine(day: TripPlanDay): string {
  const weather = day.weather;
  if (weather.coverage === "unavailable") return "暂无对应日期天气预报";
  const daytime = [weather.day_weather, weather.day_temperature && `${weather.day_temperature}℃`]
    .filter(Boolean)
    .join(" ");
  const nighttime = [
    weather.night_weather,
    weather.night_temperature && `${weather.night_temperature}℃`,
  ]
    .filter(Boolean)
    .join(" ");
  return `白天 ${daytime || "—"} · 夜间 ${nighttime || "—"}`;
}

function dayTheme(plan: TravelPlanDetail, day: TripPlanDay): string {
  return (
    plan.narrative?.days.find((item) => item.day_index === day.day_index)?.theme ??
    `第 ${day.day_index} 天行程`
  );
}

function recommendationReason(
  plan: TravelPlanDetail,
  day: TripPlanDay,
  place: TripPlanPlace,
): string {
  const narrativeDay = plan.narrative?.days.find((item) => item.day_index === day.day_index);
  return (
    narrativeDay?.places.find((item) => item.reference_id === place.reference_id)
      ?.recommendation_reason ??
    place.selection_reasons[0] ??
    "来自本次高德地图规划结果"
  );
}

export function TravelPlanView({ planId, version }: { planId: string; version?: number }) {
  const [plan, setPlan] = useState<TravelPlanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeDay, setActiveDay] = useState(0);
  const [shared, setShared] = useState(false);
  const [searchKind, setSearchKind] = useState<SearchKind | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchTravelPlan(planId, version, controller.signal)
      .then(setPlan)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "旅行计划加载失败。");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [planId, version]);

  const selectedDay = plan?.snapshot.days.find((day) => day.day_index === activeDay) ?? null;
  const mapPlaces = useMemo(
    () =>
      selectedDay
        ? selectedDay.places
        : (plan?.snapshot.days.flatMap((day) => day.places) ?? []),
    [plan, selectedDay],
  );

  const share = async () => {
    const title = plan?.title ?? "旅行计划";
    try {
      if (navigator.share) await navigator.share({ title, url: window.location.href });
      else await navigator.clipboard.writeText(window.location.href);
      setShared(true);
      window.setTimeout(() => setShared(false), 1800);
    } catch {
      // The user may cancel the native share sheet; no error state is needed.
    }
  };

  if (loading) {
    return (
      <main className="grid min-h-dvh place-items-center bg-[#f4f6f8] text-[#17202a]">
        <div className="flex items-center gap-3 text-sm text-[#697586]">
          <LoaderCircle className="animate-spin" size={18} />
          正在打开旅行计划…
        </div>
      </main>
    );
  }

  if (error || !plan) {
    return (
      <main className="grid min-h-dvh place-items-center bg-[#f4f6f8] px-5 text-[#17202a]">
        <div className="w-full max-w-md rounded-3xl border border-black/[0.06] bg-white p-7 text-center shadow-xl shadow-black/[0.05]">
          <CircleAlert className="mx-auto text-red-500" size={30} />
          <h1 className="mt-4 text-xl font-semibold">旅行计划暂时打不开</h1>
          <p className="mt-2 text-sm leading-6 text-[#697586]">{error ?? "计划不存在。"}</p>
          <Link
            className="mt-6 inline-flex items-center gap-2 rounded-xl bg-[#0f766e] px-4 py-2.5 text-sm font-medium text-white"
            href="/"
          >
            <ArrowLeft size={15} /> 返回对话
          </Link>
        </div>
      </main>
    );
  }

  const snapshot = plan.snapshot;
  const structured = snapshot.schema_version === "trip_plan.v2";
  const city = structured
    ? snapshot.request.destination_city
    : snapshot.request.core.destination_city ?? "目的地";
  const startDate = structured ? snapshot.request.start_date : snapshot.request.core.start_date;
  const duration = structured
    ? snapshot.request.duration_days
    : snapshot.request.core.duration_days ?? snapshot.days.length;
  const restaurants = structured ? snapshot.restaurant_recommendations : [];

  return (
    <main className="min-h-dvh bg-[#f4f6f8] text-[#17202a]">
      <AssistantWidget activePlanId={planId} />
      <header className="sticky top-0 z-50 border-b border-black/[0.06] bg-white/90 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-[1240px] items-center justify-between px-4 sm:px-6">
          <Link className="flex items-center gap-2 text-sm font-medium" href="/">
            <ArrowLeft size={17} />
            <span className="hidden sm:inline">返回规划台</span>
          </Link>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <span className="grid size-8 place-items-center rounded-xl bg-[#0f766e] text-white">
              <MapPinned size={16} />
            </span>
            远行计划
          </div>
          <button
            className="mr-14 flex items-center gap-2 rounded-xl border border-black/[0.08] bg-white px-3 py-2 text-xs font-medium transition-colors hover:bg-black/[0.025] sm:mr-16"
            onClick={() => void share()}
            type="button"
          >
            {shared ? <Check className="text-emerald-600" size={15} /> : <Share2 size={15} />}
            {shared ? "已复制" : "分享"}
          </button>
        </div>
      </header>

      <section className="border-b border-black/[0.05] bg-[linear-gradient(135deg,#ecfdf5_0%,#f0fdfa_42%,#eff6ff_100%)]">
        <div className="mx-auto max-w-[1240px] px-4 py-8 sm:px-6 sm:py-12">
          <div className="flex flex-wrap items-center gap-2 text-xs text-[#52606d]">
            <span className="rounded-full bg-white/75 px-3 py-1.5 shadow-sm">{city}</span>
            <span className="rounded-full bg-white/75 px-3 py-1.5 shadow-sm">
              {startDate ? formatDate(startDate) : "日期待确认"} · {duration} 天
            </span>
            <span className="rounded-full bg-white/75 px-3 py-1.5 shadow-sm">
              版本 {plan.version}
              {plan.version < plan.current_version ? ` / 最新 ${plan.current_version}` : " · 当前版本"}
            </span>
          </div>
          <h1 className="mt-5 max-w-3xl text-3xl font-semibold tracking-[-0.04em] sm:text-5xl">
            {plan.narrative?.title ?? plan.title}
          </h1>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-[#52606d] sm:text-base">
            {plan.narrative?.summary ?? "地图、路线、天气和城市餐饮推荐已整理为可执行旅行计划。"}
          </p>
          <div className="mt-6 flex flex-wrap gap-2">
            {(
              [
                ["hotel", "查询酒店", Hotel],
                ["flight", "查询航班", Plane],
                ["train", "查询火车", TrainFront],
              ] as const
            ).map(([kind, label, Icon]) => (
              <button
                className="flex items-center gap-2 rounded-xl border border-black/[0.07] bg-white/85 px-3.5 py-2.5 text-xs font-semibold text-[#425466] shadow-sm hover:bg-white"
                key={kind}
                onClick={() => setSearchKind(kind)}
                type="button"
              >
                <Icon className="text-[#0f766e]" size={14} /> {label}
              </button>
            ))}
          </div>
        </div>
      </section>

      <nav className="sticky top-16 z-40 border-b border-black/[0.06] bg-white/92 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1240px] gap-2 overflow-x-auto px-4 py-3 sm:px-6">
          <DayTab active={activeDay === 0} label="总览" onClick={() => setActiveDay(0)} />
          {snapshot.days.map((day) => (
            <DayTab
              key={day.day_id}
              active={activeDay === day.day_index}
              label={`D${day.day_index} · ${dayTheme(plan, day)}`}
              onClick={() => setActiveDay(day.day_index)}
            />
          ))}
        </div>
      </nav>

      <div className="mx-auto grid max-w-[1240px] gap-6 px-4 py-6 sm:px-6 lg:grid-cols-[minmax(0,1fr)_430px] lg:items-start lg:py-8">
        <div className="min-w-0 space-y-6">
          {selectedDay ? (
            <DayItinerary plan={plan} day={selectedDay} />
          ) : (
            <PlanOverview plan={plan} />
          )}
        </div>
        <div className="order-first lg:sticky lg:top-[132px] lg:order-last">
          <RouteMap
            day={selectedDay}
            key={selectedDay?.day_id ?? "trip-overview"}
            places={mapPlaces}
            restaurants={restaurants}
          />
        </div>
      </div>
      <TravelSearchDialog
        key={searchKind ?? "closed"}
        kind={searchKind}
        onClose={() => setSearchKind(null)}
      />
    </main>
  );
}

function DayTab({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      className={`shrink-0 rounded-full px-4 py-2 text-xs font-medium transition-colors ${
        active ? "bg-[#0f766e] text-white" : "bg-[#f1f5f4] text-[#52606d] hover:bg-[#e5eeec]"
      }`}
      onClick={onClick}
      type="button"
    >
      {label}
    </button>
  );
}

function PlanOverview({ plan }: { plan: TravelPlanDetail }) {
  const { snapshot } = plan;
  const structured = snapshot.schema_version === "trip_plan.v2";
  const practicalTips = structured ? [] : (plan.narrative?.practical_tips ?? []);
  const hasTravelOptions =
    !structured &&
    (snapshot.transport.enabled || snapshot.hotel.enabled || snapshot.transport.options.length > 0);
  return (
    <>
      <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <CalendarDays className="text-[#0f766e]" size={18} /> 每日安排
        </div>
        <div className="mt-4 space-y-3">
          {snapshot.days.map((day) => (
            <div
              key={day.day_id}
              className="rounded-2xl border border-black/[0.055] bg-[#fbfcfc] p-4"
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[#0f766e]">
                    D{day.day_index} · {formatDate(day.date)}
                  </p>
                  <h2 className="mt-1.5 text-lg font-semibold">{dayTheme(plan, day)}</h2>
                </div>
                <span className="shrink-0 rounded-full bg-[#ecfdf5] px-2.5 py-1 text-[11px] text-[#047857]">
                  {day.places.length} 站
                </span>
              </div>
              <p className="mt-3 text-xs leading-5 text-[#697586]">{weatherLine(day)}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {day.places.map((place, index) => (
                  <span
                    key={place.plan_item_id}
                    className="rounded-lg bg-[#edf2f1] px-2.5 py-1.5 text-xs text-[#425466]"
                  >
                    {index + 1}. {place.name}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>

      {structured && <RestaurantRecommendations restaurants={snapshot.restaurant_recommendations} />}
      {hasTravelOptions && !structured && <TravelOptions snapshot={snapshot} />}

      {practicalTips.length > 0 && (
        <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Sparkles className="text-amber-500" size={18} /> 出发前提醒
          </div>
          <ul className="mt-4 space-y-2.5 text-sm leading-6 text-[#52606d]">
            {practicalTips.map((tip, index) => (
              <li className="flex gap-2.5" key={`${tip}-${index}`}>
                <span className="mt-2 size-1.5 shrink-0 rounded-full bg-amber-400" />
                {tip}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function DayItinerary({ plan, day }: { plan: TravelPlanDetail; day: TripPlanDay }) {
  const structured = plan.snapshot.schema_version === "trip_plan.v2";
  const narrativeDay = plan.narrative?.days.find((item) => item.day_index === day.day_index);
  const dayTips = structured ? [] : (narrativeDay?.tips ?? []);
  const legsByDestination = new Map(
    day.route_legs.map((leg) => [leg.destination_plan_item_id, leg]),
  );
  return (
    <>
      <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
        <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[#0f766e]">
          第 {day.day_index} 天 · {formatDate(day.date)}
        </p>
        <h2 className="mt-2 text-2xl font-semibold tracking-[-0.025em]">{dayTheme(plan, day)}</h2>
        <div className="mt-5 grid gap-3 sm:grid-cols-3">
          <SummaryStat icon={<CloudSun size={17} />} label="天气" value={weatherLine(day)} />
          <SummaryStat
            icon={<Clock3 size={17} />}
            label="游玩预算"
            value={formatDuration(day.estimated_visit_minutes) ?? "待确认"}
          />
          <SummaryStat
            icon={<Route size={17} />}
            label="相邻交通"
            value={formatDuration(day.estimated_transport_minutes) ?? "待确认"}
          />
        </div>
        {day.weather.advice.length > 0 && (
          <div className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-900">
            {day.weather.advice.join("；")}
          </div>
        )}
      </section>

      <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Route className="text-[#0f766e]" size={18} /> 当日路线
        </div>
        <div className="relative mt-5 pl-9 before:absolute before:bottom-6 before:left-[15px] before:top-6 before:w-px before:bg-[#d8e2df]">
          {day.places.map((place, index) => {
            const leg = legsByDestination.get(place.plan_item_id);
            return (
              <div key={place.plan_item_id}>
                {leg && <RouteLegRow leg={leg} />}
                <article className="relative mb-5 rounded-2xl border border-black/[0.055] bg-[#fbfcfc] p-4">
                  <span className="absolute -left-[34px] top-5 grid size-7 place-items-center rounded-full border-4 border-white bg-[#0f766e] text-[10px] font-bold text-white shadow-sm">
                    {index + 1}
                  </span>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-[11px] font-medium text-[#8090a0]">
                        第 {index + 1} 站 · 预计 {place.estimated_visit_minutes} 分钟
                      </p>
                      <h3 className="mt-1 text-lg font-semibold">{place.name}</h3>
                    </div>
                    <span className="shrink-0 rounded-lg bg-[#edf2f1] px-2 py-1 text-[10px] text-[#52606d]">
                      {place.poi_type || "景点"}
                    </span>
                  </div>
                  <p className="mt-2 text-xs leading-5 text-[#697586]">
                    {place.address || "高德未提供详细地址"}
                  </p>
                  <p className="mt-3 border-l-2 border-[#5eead4] pl-3 text-sm leading-6 text-[#425466]">
                    {recommendationReason(plan, day, place)}
                  </p>
                  <a
                    className="mt-4 inline-flex items-center gap-2 rounded-xl bg-[#0f766e] px-3.5 py-2.5 text-xs font-semibold text-white shadow-sm transition-transform hover:-translate-y-0.5"
                    href={amapPlaceUrl(place)}
                    rel="noreferrer noopener"
                    target="_blank"
                  >
                    <Navigation size={14} /> 高德导航
                  </a>
                </article>
              </div>
            );
          })}
        </div>
      </section>

      {dayTips.length > 0 && (
        <section className="rounded-3xl border border-amber-200/60 bg-amber-50 p-5 sm:p-6">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-900">
            <TriangleAlert size={17} /> 当天提醒
          </div>
          <ul className="mt-3 space-y-2 text-sm leading-6 text-amber-900/80">
            {dayTips.map((tip, index) => (
              <li key={`${tip}-${index}`}>· {tip}</li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

function SummaryStat({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-[#f4f7f6] p-3.5">
      <div className="flex items-center gap-2 text-[11px] font-medium text-[#8090a0]">
        {icon} {label}
      </div>
      <p className="mt-2 text-xs font-medium leading-5 text-[#425466]">{value}</p>
    </div>
  );
}

function RouteLegRow({ leg }: { leg: TripPlanRouteLeg }) {
  const facts = [
    routeModeLabel(leg.mode),
    formatDistance(leg.distance_meters),
    leg.duration_seconds ? formatDuration(Math.max(1, Math.round(leg.duration_seconds / 60))) : null,
    leg.transfer_count !== null ? `换乘 ${leg.transfer_count} 次` : null,
  ].filter(Boolean);
  return (
    <div className="relative -ml-5 mb-3 flex items-center gap-2 py-1 text-[11px] text-[#8090a0]">
      <BusFront className="relative z-10 bg-white text-[#0f766e]" size={16} />
      <span>{facts.join(" · ")}</span>
      {leg.is_fallback && <span className="rounded bg-amber-50 px-1.5 text-amber-700">估算</span>}
    </div>
  );
}

function RestaurantRecommendations({ restaurants }: { restaurants: RestaurantRecommendation[] }) {
  return (
    <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Utensils className="text-orange-500" size={18} /> 城市餐饮推荐
        </div>
        <span className="text-[11px] text-[#8090a0]">整体推荐，不计入每日路线</span>
      </div>
      {restaurants.length === 0 ? (
        <p className="mt-4 rounded-2xl bg-[#f7f9f8] p-4 text-xs leading-5 text-[#697586]">
          本次没有补充未经验证的餐厅，核心行程不受影响。
        </p>
      ) : (
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          {restaurants.map((restaurant) => (
            <article
              className="rounded-2xl border border-black/[0.055] bg-[#fffaf5] p-4"
              key={restaurant.provider_place_id}
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-semibold">{restaurant.name}</h3>
                {restaurant.rating !== null && (
                  <span className="shrink-0 rounded-full bg-orange-100 px-2 py-1 text-[10px] font-semibold text-orange-700">
                    {restaurant.rating} 分
                  </span>
                )}
              </div>
              <p className="mt-2 text-xs leading-5 text-[#697586]">
                {[restaurant.business_area, restaurant.address].filter(Boolean).join(" · ") || "地址待确认"}
              </p>
              <p className="mt-3 text-xs leading-5 text-[#52606d]">
                {restaurant.recommendation_reason}
              </p>
              <a
                className="mt-4 inline-flex items-center gap-1.5 rounded-xl bg-orange-500 px-3 py-2 text-xs font-semibold text-white"
                href={amapPlaceUrl(restaurant)}
                rel="noreferrer noopener"
                target="_blank"
              >
                地图查看 <Navigation size={12} />
              </a>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function TravelOptions({ snapshot }: { snapshot: TravelPlanSnapshotV1 }) {
  const { transport, hotel } = snapshot;
  return (
    <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Plane className="text-[#0f766e]" size={18} /> 交通与住宿候选
        </div>
        <span className="text-[11px] text-[#8090a0]">展示查询结果，不代替用户选择</span>
      </div>

      {transport.enabled && (
        <CapabilityGroup
          emptyText={capabilityEmptyText("交通", transport.status, transport.warnings)}
          icon={<TrainFront size={16} />}
          title="车票与航班"
        >
          {transport.options.map((option) => (
            <TransportOptionCard key={option.option_id} option={option} />
          ))}
        </CapabilityGroup>
      )}

      {hotel.enabled && (
        <CapabilityGroup
          emptyText={capabilityEmptyText("酒店", hotel.status, hotel.warnings)}
          icon={<Hotel size={16} />}
          title="酒店"
        >
          {hotel.options.map((option) => (
            <HotelOptionCard key={option.option_id} option={option} />
          ))}
        </CapabilityGroup>
      )}

      <div className="mt-5 rounded-2xl bg-[#f7f8fa] px-4 py-3 text-[11px] leading-5 text-[#697586]">
        数据主要来自飞猪查询结果
        {formatQueryTime(transport.queried_at ?? hotel.queried_at)
          ? ` · 查询于 ${formatQueryTime(transport.queried_at ?? hotel.queried_at)}`
          : ""}
        。票价、房价、余票、库存和退改规则可能变化，请点击后以飞猪实时页面为准。
      </div>
    </section>
  );
}

function CapabilityGroup({
  icon,
  title,
  emptyText,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  emptyText: string | null;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-6">
      <h3 className="flex items-center gap-2 text-xs font-semibold text-[#52606d]">
        {icon} {title}
      </h3>
      {emptyText ? (
        <div className="mt-3 flex items-start gap-2 rounded-2xl bg-amber-50 p-4 text-xs leading-5 text-amber-900">
          <TriangleAlert className="mt-0.5 shrink-0" size={15} /> {emptyText}
        </div>
      ) : (
        <div className="mt-3 grid gap-3">{children}</div>
      )}
    </div>
  );
}

function capabilityEmptyText(
  label: string,
  status: "skipped" | "usable" | "empty" | "failed",
  warnings: string[],
): string | null {
  if (status === "usable") return null;
  if (warnings.length) return warnings.join("；");
  if (status === "empty") return `本次没有查询到可展示的${label}候选。`;
  if (status === "failed") return `${label}查询暂时失败，请稍后重新生成计划。`;
  return `${label}查询未执行。`;
}

function TransportOptionCard({ option }: { option: TransportOption }) {
  const detailUrl = safeDetailUrl(option.detail_url);
  const title = [
    ...option.transport_names,
    ...option.transport_numbers,
  ].filter(Boolean).join(" · ");
  const tags = [
    option.direction === "return" ? "返程" : "去程",
    option.journey_type,
    formatDuration(option.duration_minutes),
    option.seat_classes.join(" / ") || null,
  ].filter(Boolean);
  return (
    <article className="rounded-2xl border border-black/[0.055] bg-[#fbfcfc] p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {option.mode === "flight" ? (
              <Plane className="text-sky-600" size={16} />
            ) : (
              <TrainFront className="text-violet-600" size={16} />
            )}
            <h4 className="truncate text-sm font-semibold">{title || "出行候选"}</h4>
          </div>
          <p className="mt-3 text-sm font-medium">
            {option.departure_station} <span className="text-[#8090a0]">{option.departure_at}</span>
          </p>
          <p className="my-1 text-[11px] text-[#a0aab5]">↓</p>
          <p className="text-sm font-medium">
            {option.arrival_station} <span className="text-[#8090a0]">{option.arrival_at}</span>
          </p>
        </div>
        {formatPrice(option.price_amount) && (
          <span className="shrink-0 text-sm font-semibold text-orange-600">
            {formatPrice(option.price_amount)}
          </span>
        )}
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {tags.map((tag) => (
          <span className="rounded-md bg-[#edf2f1] px-2 py-1 text-[10px] text-[#52606d]" key={tag}>
            {tag}
          </span>
        ))}
      </div>
      {detailUrl && <FliggyLink href={detailUrl} />}
    </article>
  );
}

function HotelOptionCard({ option }: { option: HotelOption }) {
  const detailUrl = safeDetailUrl(option.detail_url);
  return (
    <article className="rounded-2xl border border-black/[0.055] bg-[#fbfcfc] p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <BedDouble className="text-amber-600" size={16} />
            <h4 className="text-sm font-semibold">{option.name}</h4>
          </div>
          <p className="mt-2 text-xs leading-5 text-[#697586]">
            {[option.star, option.nearby_poi].filter(Boolean).join(" · ") || "住宿候选"}
          </p>
          {option.address && <p className="mt-1 text-xs leading-5 text-[#8090a0]">{option.address}</p>}
        </div>
        {formatPrice(option.price_amount) && (
          <span className="shrink-0 text-sm font-semibold text-orange-600">
            {formatPrice(option.price_amount)}
          </span>
        )}
      </div>
      {detailUrl && <FliggyLink href={detailUrl} />}
    </article>
  );
}

function FliggyLink({ href }: { href: string }) {
  return (
    <a
      className="mt-4 inline-flex items-center gap-2 rounded-xl bg-[#ff6a00] px-3.5 py-2.5 text-xs font-semibold text-white transition-transform hover:-translate-y-0.5"
      href={href}
      rel="noreferrer noopener"
      target="_blank"
    >
      去飞猪查看 <ExternalLink size={13} />
    </a>
  );
}

type MapLoadStatus = "idle" | "loading" | "ready" | "failed";
type RouteSearchStatus = "idle" | "loading" | "ready" | "failed";
type SelectedRouteRole = "origin" | "destination" | null;

type RouteSearchResult = {
  requestKey: string;
  status: "ready" | "failed";
  detail: string;
};

function markerContent(index: number, role: SelectedRouteRole): string {
  const label = role === "origin" ? "起" : role === "destination" ? "终" : String(index + 1);
  const background = role === "origin" ? "#f59e0b" : role === "destination" ? "#ef4444" : "#0f766e";
  return `<div style="display:grid;place-items:center;width:32px;height:32px;border-radius:999px;background:${background};color:white;border:3px solid white;box-shadow:0 4px 14px rgba(15,23,42,.28);font:700 11px system-ui;cursor:pointer">${label}</div>`;
}

function restaurantMarkerContent(): string {
  return '<div style="display:grid;place-items:center;width:30px;height:30px;border-radius:10px;background:#f97316;color:white;border:3px solid white;box-shadow:0 4px 14px rgba(15,23,42,.24);font:700 14px system-ui">餐</div>';
}

function routeMetric(value: number | string | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.round(parsed) : null;
}

function RouteMap({
  places,
  restaurants,
  day,
}: {
  places: TripPlanPlace[];
  restaurants: RestaurantRecommendation[];
  day: TripPlanDay | null;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<AmapMapInstance | null>(null);
  const amapRef = useRef<AmapApi | null>(null);
  const markersRef = useRef<Map<string, AmapMarkerInstance>>(new Map());
  const restaurantMarkersRef = useRef<Map<string, AmapMarkerInstance>>(new Map());
  const routeServiceRef = useRef<AmapRouteService | null>(null);
  const [mapStatus, setMapStatus] = useState<MapLoadStatus>("idle");
  const [mapError, setMapError] = useState<string | null>(null);
  const [mapAttempt, setMapAttempt] = useState(0);
  const [selectedPlaceIds, setSelectedPlaceIds] = useState<string[]>([]);
  const [routeMode, setRouteMode] = useState<InteractiveRouteMode>("walking");
  const [routeResult, setRouteResult] = useState<RouteSearchResult | null>(null);
  const [showRestaurants, setShowRestaurants] = useState(true);
  const key = process.env.NEXT_PUBLIC_AMAP_JS_KEY?.trim();
  const securityCode = process.env.NEXT_PUBLIC_AMAP_SECURITY_CODE?.trim();

  const selectPlace = useCallback((placeId: string) => {
    setSelectedPlaceIds((current) => {
      if (current.includes(placeId)) return current.filter((item) => item !== placeId);
      if (current.length >= 2) return [placeId];
      return [...current, placeId];
    });
  }, []);

  const selectedPlaces = useMemo(
    () =>
      selectedPlaceIds
        .map((placeId) => places.find((place) => place.plan_item_id === placeId))
        .filter((place): place is TripPlanPlace => Boolean(place)),
    [places, selectedPlaceIds],
  );

  useEffect(() => {
    if (!key || !hostRef.current || places.length === 0) {
      setMapStatus("idle");
      return;
    }
    let active = true;
    let map: AmapMapInstance | null = null;
    let mapReadyTimeoutId: number | null = null;
    setMapStatus("loading");
    setMapError(null);

    loadAmap(key, securityCode)
      .then(async (AMap) => {
        if (!active) return null;
        await validateAmapKey(AMap, key);
        return AMap;
      })
      .then((AMap) => {
        if (!AMap || !active || !hostRef.current) return;
        map = new AMap.Map(hostRef.current, {
          zoom: 12,
          viewMode: "2D",
          mapStyle: "amap://styles/whitesmoke",
          resizeEnable: true,
        });
        mapRef.current = map;
        amapRef.current = AMap;
        map.on("complete", () => {
          if (mapReadyTimeoutId !== null) window.clearTimeout(mapReadyTimeoutId);
          if (active) setMapStatus("ready");
        });
        mapReadyTimeoutId = window.setTimeout(() => {
          if (!active) return;
          setMapError("高德底图响应超时，请检查 Web端（JS API）Key、安全密钥和域名白名单。");
          setMapStatus("failed");
        }, 12_000);

        const markerEntries = places.map((place, index) => {
          const marker = new AMap.Marker({
            position: [place.location.longitude, place.location.latitude],
            anchor: "center",
            content: markerContent(index, null),
            title: `${place.name}（点击选择路线起终点）`,
          });
          marker.on("click", () => selectPlace(place.plan_item_id));
          return [place.plan_item_id, marker] as const;
        });
        const markers = markerEntries.map(([, marker]) => marker);
        markersRef.current = new Map(markerEntries);
        const restaurantEntries = showRestaurants
          ? restaurants.map((restaurant) => {
              const marker = new AMap.Marker({
                position: [restaurant.location.longitude, restaurant.location.latitude],
                anchor: "center",
                content: restaurantMarkerContent(),
                title: `${restaurant.name}（餐饮推荐）`,
              });
              return [restaurant.provider_place_id, marker] as const;
            })
          : [];
        const restaurantMarkers = restaurantEntries.map(([, marker]) => marker);
        restaurantMarkersRef.current = new Map(restaurantEntries);
        const overlays: object[] = [...markers, ...restaurantMarkers];
        if (places.length > 1) {
          overlays.push(
            new AMap.Polyline({
              path: places.map((place) => [
                place.location.longitude,
                place.location.latitude,
              ]),
              strokeColor: "#0f766e",
              strokeWeight: 3,
              strokeOpacity: 0.38,
              strokeStyle: "dashed",
              lineJoin: "round",
            }),
          );
        }
        map.add(overlays);
        map.setFitView([...markers, ...restaurantMarkers], false, [72, 52, 72, 52]);
      })
      .catch((reason: unknown) => {
        if (mapReadyTimeoutId !== null) window.clearTimeout(mapReadyTimeoutId);
        map?.destroy();
        map = null;
        mapRef.current = null;
        amapRef.current = null;
        markersRef.current.clear();
        restaurantMarkersRef.current.clear();
        if (!active) return;
        setMapError(reason instanceof Error ? reason.message : "高德地图加载失败。");
        setMapStatus("failed");
      });

    return () => {
      active = false;
      if (mapReadyTimeoutId !== null) window.clearTimeout(mapReadyTimeoutId);
      routeServiceRef.current?.clear();
      routeServiceRef.current = null;
      markersRef.current.clear();
      restaurantMarkersRef.current.clear();
      if (mapRef.current === map) mapRef.current = null;
      amapRef.current = null;
      map?.destroy();
    };
  }, [key, mapAttempt, places, restaurants, securityCode, selectPlace, showRestaurants]);

  useEffect(() => {
    places.forEach((place, index) => {
      const selectedIndex = selectedPlaceIds.indexOf(place.plan_item_id);
      const role: SelectedRouteRole =
        selectedIndex === 0 ? "origin" : selectedIndex === 1 ? "destination" : null;
      markersRef.current.get(place.plan_item_id)?.setContent(markerContent(index, role));
    });
  }, [mapStatus, places, selectedPlaceIds]);

  useEffect(() => {
    routeServiceRef.current?.clear();
    routeServiceRef.current = null;

    const map = mapRef.current;
    const AMap = amapRef.current;
    if (mapStatus !== "ready" || !map || !AMap) return;
    if (selectedPlaces.length !== 2) {
      const markers = [
        ...markersRef.current.values(),
        ...restaurantMarkersRef.current.values(),
      ];
      if (markers.length > 0) map.setFitView(markers, false, [72, 52, 72, 52]);
      return;
    }

    let active = true;
    const [origin, destination] = selectedPlaces;
    const pluginName = routeMode === "walking" ? "AMap.Walking" : "AMap.Driving";
    const requestKey = `${origin.plan_item_id}:${destination.plan_item_id}:${routeMode}`;
    const timeoutId = window.setTimeout(() => {
      if (!active) return;
      setRouteResult({
        requestKey,
        status: "failed",
        detail: "路线计算超时，可点击下方按钮前往高德继续查看。",
      });
    }, 15_000);

    try {
      AMap.plugin(pluginName, () => {
        if (!active || !mapRef.current) return;
        try {
          const RouteService = routeMode === "walking" ? AMap.Walking : AMap.Driving;
          if (!RouteService) throw new Error("路线规划插件未加载");
          const service = new RouteService({
            map: mapRef.current,
            hideMarkers: true,
            showTraffic: routeMode === "driving",
          });
          routeServiceRef.current = service;
          service.search(
            [origin.location.longitude, origin.location.latitude],
            [destination.location.longitude, destination.location.latitude],
            (status, result) => {
              if (!active) return;
              window.clearTimeout(timeoutId);
              if (status !== "complete" || typeof result === "string") {
                setRouteResult({
                  requestKey,
                  status: "failed",
                  detail: "暂时无法取得这两个景点的路线，可前往高德继续规划。",
                });
                return;
              }
              const primaryRoute = result.routes?.[0];
              const distance = routeMetric(primaryRoute?.distance);
              const durationSeconds = routeMetric(primaryRoute?.time);
              const facts = [
                distance === null ? null : formatDistance(distance),
                durationSeconds === null
                  ? null
                  : formatDuration(Math.max(1, Math.round(durationSeconds / 60))),
              ].filter(Boolean);
              setRouteResult({
                requestKey,
                status: "ready",
                detail:
                  facts.length > 0
                    ? `${routeMode === "walking" ? "步行" : "驾车"} · ${facts.join(" · ")}`
                    : "路线已绘制在地图上。",
              });
            },
          );
        } catch {
          window.clearTimeout(timeoutId);
          if (!active) return;
          setRouteResult({
            requestKey,
            status: "failed",
            detail: "路线规划插件加载失败，可前往高德继续规划。",
          });
        }
      });
    } catch {
      window.clearTimeout(timeoutId);
      window.queueMicrotask(() => {
        if (!active) return;
        setRouteResult({
          requestKey,
          status: "failed",
          detail: "路线规划插件加载失败，可前往高德继续规划。",
        });
      });
    }

    return () => {
      active = false;
      window.clearTimeout(timeoutId);
      routeServiceRef.current?.clear();
      routeServiceRef.current = null;
    };
  }, [mapStatus, routeMode, selectedPlaces]);

  const routeUrl =
    selectedPlaces.length === 2
      ? amapRouteUrl(selectedPlaces[0], selectedPlaces[1], routeMode)
      : null;
  const routeRequestKey =
    selectedPlaces.length === 2
      ? `${selectedPlaces[0].plan_item_id}:${selectedPlaces[1].plan_item_id}:${routeMode}`
      : null;
  const currentRouteResult =
    routeRequestKey && routeResult?.requestKey === routeRequestKey ? routeResult : null;
  const routeStatus: RouteSearchStatus = !routeRequestKey
    ? "idle"
    : currentRouteResult?.status ?? "loading";
  const routeDetail =
    currentRouteResult?.detail ?? `${routeMode === "walking" ? "步行" : "驾车"}路线计算中…`;
  const fallback = !key || mapStatus === "failed" || places.length === 0;
  const loadingMap = Boolean(key) && places.length > 0 && mapStatus !== "ready" && !fallback;

  return (
    <section className="overflow-hidden rounded-3xl border border-black/[0.055] bg-white shadow-sm">
      <div className="flex items-center justify-between gap-3 border-b border-black/[0.055] px-5 py-4">
        <div>
          <p className="text-sm font-semibold">{day ? `D${day.day_index} 路线地图` : "行程地图"}</p>
          <p className="mt-0.5 text-[11px] text-[#8090a0]">
            {places.length} 个景点
            {restaurants.length > 0 ? ` · ${restaurants.length} 家餐厅` : ""} · 点击两个景点规划路线
          </p>
        </div>
        <div className="flex items-center gap-2">
          {restaurants.length > 0 && (
            <button
              aria-pressed={showRestaurants}
              className={`rounded-xl px-2.5 py-1.5 text-[10px] font-medium ${
                showRestaurants ? "bg-orange-100 text-orange-700" : "bg-[#f1f5f4] text-[#697586]"
              }`}
              onClick={() => setShowRestaurants((value) => !value)}
              type="button"
            >
              餐饮图层
            </button>
          )}
          <MapPinned className="text-[#0f766e]" size={18} />
        </div>
      </div>

      <div className="travel-plan-map-frame">
        <div aria-label="高德行程地图" className="travel-plan-map-canvas" ref={hostRef} />
        {loadingMap && (
          <div className="absolute inset-0 grid place-items-center bg-[#edf3f1] text-xs text-[#697586]">
            <span className="flex items-center gap-2">
              <LoaderCircle className="animate-spin" size={15} /> 正在加载高德地图
            </span>
          </div>
        )}
        {fallback && (
          <MapFallback
            configured={Boolean(key)}
            error={mapError}
            onRetry={() => setMapAttempt((attempt) => attempt + 1)}
            onSelect={selectPlace}
            places={places}
            selectedPlaceIds={selectedPlaceIds}
          />
        )}
      </div>

      <div className="border-t border-black/[0.055] p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-xs font-semibold">两点路线规划</p>
            <p className="mt-1 text-[11px] leading-4 text-[#8090a0]">
              {selectedPlaces.length === 0 && "请先选择起点，再选择终点。"}
              {selectedPlaces.length === 1 && `已选起点：${selectedPlaces[0].name}，请继续选择终点。`}
              {selectedPlaces.length === 2 &&
                `${selectedPlaces[0].name} → ${selectedPlaces[1].name}`}
            </p>
          </div>
          <div className="flex shrink-0 rounded-xl bg-[#f1f5f4] p-1 text-[10px] font-medium">
            {(["walking", "driving"] as const).map((mode) => (
              <button
                className={`rounded-lg px-2.5 py-1.5 transition-colors ${
                  routeMode === mode ? "bg-white text-[#0f766e] shadow-sm" : "text-[#697586]"
                }`}
                key={mode}
                onClick={() => setRouteMode(mode)}
                type="button"
              >
                {mode === "walking" ? "步行" : "驾车"}
              </button>
            ))}
          </div>
        </div>

        <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
          {places.map((place, index) => {
            const selectedIndex = selectedPlaceIds.indexOf(place.plan_item_id);
            const selected = selectedIndex >= 0;
            return (
              <button
                aria-pressed={selected}
                className={`flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2 text-[11px] font-medium transition-colors ${
                  selected
                    ? selectedIndex === 0
                      ? "border-amber-300 bg-amber-50 text-amber-900"
                      : "border-red-200 bg-red-50 text-red-800"
                    : "border-black/[0.07] bg-white text-[#52606d] hover:bg-[#f4f7f6]"
                }`}
                key={place.plan_item_id}
                onClick={() => selectPlace(place.plan_item_id)}
                type="button"
              >
                <span
                  className={`grid size-5 place-items-center rounded-full text-[9px] font-bold text-white ${
                    selectedIndex === 0
                      ? "bg-amber-500"
                      : selectedIndex === 1
                        ? "bg-red-500"
                        : "bg-[#0f766e]"
                  }`}
                >
                  {selectedIndex === 0 ? "起" : selectedIndex === 1 ? "终" : index + 1}
                </span>
                {place.name}
              </button>
            );
          })}
        </div>

        {selectedPlaces.length === 2 && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded-2xl bg-[#f4f7f6] px-3.5 py-3">
            <div className="min-w-0 text-[11px] leading-5 text-[#52606d]">
              {routeStatus === "loading" && (
                <span className="flex items-center gap-2">
                  <LoaderCircle className="animate-spin" size={13} /> {routeDetail}
                </span>
              )}
              {routeStatus !== "loading" && (
                <span className={routeStatus === "failed" ? "text-amber-700" : ""}>
                  {mapStatus === "ready" ? routeDetail : "内嵌地图恢复后会自动绘制路线。"}
                </span>
              )}
            </div>
            {routeUrl && (
              <a
                className="flex shrink-0 items-center gap-1.5 rounded-xl bg-[#0f766e] px-3 py-2 text-[10px] font-semibold text-white"
                href={routeUrl}
                rel="noreferrer noopener"
                target="_blank"
              >
                高德打开 <ExternalLink size={12} />
              </a>
            )}
          </div>
        )}

        {selectedPlaceIds.length > 0 && (
          <button
            className="mt-3 text-[10px] font-medium text-[#697586] underline underline-offset-4"
            onClick={() => setSelectedPlaceIds([])}
            type="button"
          >
            清除选择
          </button>
        )}
      </div>

      <div className="border-t border-black/[0.055] px-5 py-3 text-[10px] leading-4 text-[#8090a0]">
        虚线表示行程游览顺序；选择两个景点后，高德会按步行或驾车方式绘制实际路线。
      </div>
    </section>
  );
}

function MapFallback({
  places,
  configured,
  error,
  selectedPlaceIds,
  onSelect,
  onRetry,
}: {
  places: TripPlanPlace[];
  configured: boolean;
  error: string | null;
  selectedPlaceIds: string[];
  onSelect: (placeId: string) => void;
  onRetry: () => void;
}) {
  return (
    <div className="absolute inset-0 overflow-y-auto bg-[radial-gradient(circle_at_30%_20%,#d1fae5_0%,transparent_34%),radial-gradient(circle_at_75%_70%,#dbeafe_0%,transparent_40%),#edf3f1] p-5">
      <div className="absolute inset-0 opacity-25 [background-image:linear-gradient(#94a3b8_1px,transparent_1px),linear-gradient(90deg,#94a3b8_1px,transparent_1px)] [background-size:36px_36px]" />
      <div className="relative flex min-h-full flex-col justify-center gap-3">
        {(error || !configured) && places.length > 0 && (
          <div className="rounded-2xl border border-amber-200/70 bg-amber-50/95 px-4 py-3 text-[10px] leading-4 text-amber-900 shadow-sm">
            <p className="font-semibold">
              {configured ? "高德地图没有成功加载" : "前端尚未读取到高德 JS Key"}
            </p>
            <p className="mt-1">
              {error ?? "保存 apps/web/.env.local 后，需要重新启动前端开发服务。"}
            </p>
            {configured && (
              <button className="mt-2 font-semibold underline underline-offset-4" onClick={onRetry} type="button">
                重新加载地图
              </button>
            )}
          </div>
        )}
        {places.slice(0, 6).map((place, index) => {
          const selectedIndex = selectedPlaceIds.indexOf(place.plan_item_id);
          return (
            <button
              aria-pressed={selectedIndex >= 0}
              className={`flex items-center gap-3 rounded-2xl border bg-white/90 p-3 text-left shadow-sm backdrop-blur transition-transform hover:translate-x-1 ${
                selectedIndex >= 0 ? "border-[#0f766e]/40" : "border-white/70"
              }`}
              key={place.plan_item_id}
              onClick={() => onSelect(place.plan_item_id)}
              type="button"
            >
              <span
                className={`grid size-7 shrink-0 place-items-center rounded-full text-[10px] font-bold text-white ${
                  selectedIndex === 0
                    ? "bg-amber-500"
                    : selectedIndex === 1
                      ? "bg-red-500"
                      : "bg-[#0f766e]"
                }`}
              >
                {selectedIndex === 0 ? "起" : selectedIndex === 1 ? "终" : index + 1}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs font-medium">{place.name}</span>
              <ChevronRight className="text-[#8090a0]" size={14} />
            </button>
          );
        })}
        {places.length === 0 && (
          <div className="text-center text-xs text-[#697586]">当前没有可显示的地点坐标。</div>
        )}
      </div>
    </div>
  );
}
