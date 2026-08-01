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
import { useEffect, useMemo, useRef, useState } from "react";

import {
  fetchTravelPlan,
  type HotelOption,
  type TransportOption,
  type TravelPlanDetail,
  type TripPlanDay,
  type TripPlanPlace,
  type TripPlanRouteLeg,
} from "@/lib/api";

type AmapMapInstance = {
  add: (overlays: object[]) => void;
  fitView: (overlays?: object[], immediately?: boolean, padding?: number[]) => void;
  destroy: () => void;
};

type AmapApi = {
  Map: new (
    container: HTMLElement,
    options: { zoom: number; viewMode: "2D"; mapStyle: string },
  ) => AmapMapInstance;
  Marker: new (options: {
    position: [number, number];
    anchor: "center";
    content: string;
    title: string;
  }) => object;
  Polyline: new (options: {
    path: Array<[number, number]>;
    strokeColor: string;
    strokeWeight: number;
    strokeOpacity: number;
    strokeStyle: "dashed";
    lineJoin: "round";
  }) => object;
};

declare global {
  interface Window {
    AMap?: AmapApi;
    _AMapSecurityConfig?: { securityJsCode: string };
  }
}

let amapLoader: Promise<AmapApi> | null = null;

function loadAmap(key: string, securityCode?: string): Promise<AmapApi> {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (amapLoader) return amapLoader;
  if (securityCode) window._AMapSecurityConfig = { securityJsCode: securityCode };

  amapLoader = new Promise<AmapApi>((resolve, reject) => {
    const finish = () => {
      if (window.AMap) resolve(window.AMap);
      else reject(new Error("高德地图脚本未正确加载。"));
    };
    const existing = document.querySelector<HTMLScriptElement>("script[data-tour-amap]");
    if (existing) {
      existing.addEventListener("load", finish, { once: true });
      existing.addEventListener("error", () => reject(new Error("高德地图加载失败。")), {
        once: true,
      });
      return;
    }

    const script = document.createElement("script");
    script.dataset.tourAmap = "true";
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}`;
    script.async = true;
    script.addEventListener("load", finish, { once: true });
    script.addEventListener("error", () => reject(new Error("高德地图加载失败。")), {
      once: true,
    });
    document.head.appendChild(script);
  });
  return amapLoader;
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

function amapPlaceUrl(place: TripPlanPlace): string {
  const position = `${place.location.longitude},${place.location.latitude}`;
  return `https://uri.amap.com/marker?position=${position}&name=${encodeURIComponent(place.name)}&src=tour-assistant&coordinate=gaode&callnative=1`;
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
  const city = snapshot.request.core.destination_city ?? "目的地";
  const startDate = snapshot.request.core.start_date;
  const duration = snapshot.request.core.duration_days ?? snapshot.days.length;

  return (
    <main className="min-h-dvh bg-[#f4f6f8] text-[#17202a]">
      <header className="sticky top-0 z-50 border-b border-black/[0.06] bg-white/90 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-[1240px] items-center justify-between px-4 sm:px-6">
          <Link className="flex items-center gap-2 text-sm font-medium" href="/">
            <ArrowLeft size={17} />
            <span className="hidden sm:inline">返回对话</span>
          </Link>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <span className="grid size-8 place-items-center rounded-xl bg-[#0f766e] text-white">
              <MapPinned size={16} />
            </span>
            远行计划
          </div>
          <button
            className="flex items-center gap-2 rounded-xl border border-black/[0.08] bg-white px-3 py-2 text-xs font-medium transition-colors hover:bg-black/[0.025]"
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
            {plan.narrative?.summary ?? "地图、路线、天气和出行候选已整理为可执行旅行计划。"}
          </p>
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
          <RouteMap places={mapPlaces} day={selectedDay} />
        </div>
      </div>
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
  const hasTravelOptions =
    snapshot.transport.enabled || snapshot.hotel.enabled || snapshot.transport.options.length > 0;
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

      {hasTravelOptions && <TravelOptions plan={plan} />}

      {(plan.narrative?.practical_tips.length || snapshot.warnings.length) && (
        <section className="rounded-3xl border border-black/[0.055] bg-white p-5 shadow-sm sm:p-6">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Sparkles className="text-amber-500" size={18} /> 出发前提醒
          </div>
          <ul className="mt-4 space-y-2.5 text-sm leading-6 text-[#52606d]">
            {[...(plan.narrative?.practical_tips ?? []), ...snapshot.warnings].map((tip) => (
              <li className="flex gap-2.5" key={tip}>
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
  const narrativeDay = plan.narrative?.days.find((item) => item.day_index === day.day_index);
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

      {((narrativeDay?.tips.length ?? 0) > 0 || day.warnings.length > 0) && (
        <section className="rounded-3xl border border-amber-200/60 bg-amber-50 p-5 sm:p-6">
          <div className="flex items-center gap-2 text-sm font-semibold text-amber-900">
            <TriangleAlert size={17} /> 当天提醒
          </div>
          <ul className="mt-3 space-y-2 text-sm leading-6 text-amber-900/80">
            {[...(narrativeDay?.tips ?? []), ...day.warnings].map((tip) => (
              <li key={tip}>· {tip}</li>
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

function TravelOptions({ plan }: { plan: TravelPlanDetail }) {
  const { transport, hotel } = plan.snapshot;
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

function RouteMap({ places, day }: { places: TripPlanPlace[]; day: TripPlanDay | null }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [mapReady, setMapReady] = useState(false);
  const [mapFailed, setMapFailed] = useState(false);
  const key = process.env.NEXT_PUBLIC_AMAP_JS_KEY;
  const securityCode = process.env.NEXT_PUBLIC_AMAP_SECURITY_CODE;

  useEffect(() => {
    if (!key || !hostRef.current || places.length === 0) return;
    let active = true;
    let map: AmapMapInstance | null = null;
    setMapReady(false);
    setMapFailed(false);
    loadAmap(key, securityCode)
      .then((AMap) => {
        if (!active || !hostRef.current) return;
        map = new AMap.Map(hostRef.current, {
          zoom: 12,
          viewMode: "2D",
          mapStyle: "amap://styles/whitesmoke",
        });
        const markers = places.map(
          (place, index) =>
            new AMap.Marker({
              position: [place.location.longitude, place.location.latitude],
              anchor: "center",
              content: `<div style="display:grid;place-items:center;width:30px;height:30px;border-radius:999px;background:#0f766e;color:white;border:3px solid white;box-shadow:0 4px 14px rgba(15,118,110,.35);font:700 11px system-ui">${index + 1}</div>`,
              title: place.name,
            }),
        );
        const overlays: object[] = [...markers];
        if (places.length > 1) {
          overlays.push(
            new AMap.Polyline({
              path: places.map((place) => [
                place.location.longitude,
                place.location.latitude,
              ]),
              strokeColor: "#0f766e",
              strokeWeight: 4,
              strokeOpacity: 0.58,
              strokeStyle: "dashed",
              lineJoin: "round",
            }),
          );
        }
        map.add(overlays);
        map.fitView(markers, false, [72, 52, 72, 52]);
        setMapReady(true);
      })
      .catch(() => {
        if (active) setMapFailed(true);
      });
    return () => {
      active = false;
      map?.destroy();
    };
  }, [key, securityCode, places]);

  const fallback = !key || mapFailed;
  return (
    <section className="overflow-hidden rounded-3xl border border-black/[0.055] bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-black/[0.055] px-5 py-4">
        <div>
          <p className="text-sm font-semibold">{day ? `D${day.day_index} 路线地图` : "行程地图"}</p>
          <p className="mt-0.5 text-[11px] text-[#8090a0]">
            {places.length} 个地点 · 标记按游览顺序排列
          </p>
        </div>
        <MapPinned className="text-[#0f766e]" size={18} />
      </div>
      <div className="relative h-[340px] sm:h-[420px] lg:h-[520px]">
        {!fallback && <div className="absolute inset-0" ref={hostRef} />}
        {!fallback && !mapReady && (
          <div className="absolute inset-0 grid place-items-center bg-[#edf3f1] text-xs text-[#697586]">
            <span className="flex items-center gap-2">
              <LoaderCircle className="animate-spin" size={15} /> 正在加载高德地图
            </span>
          </div>
        )}
        {fallback && <MapFallback places={places} configured={Boolean(key)} />}
      </div>
      <div className="border-t border-black/[0.055] px-5 py-3 text-[10px] leading-4 text-[#8090a0]">
        虚线表示计划中的游览顺序；实际道路与实时交通请点击地点卡片后在高德地图确认。
      </div>
    </section>
  );
}

function MapFallback({ places, configured }: { places: TripPlanPlace[]; configured: boolean }) {
  return (
    <div className="absolute inset-0 overflow-hidden bg-[radial-gradient(circle_at_30%_20%,#d1fae5_0%,transparent_34%),radial-gradient(circle_at_75%_70%,#dbeafe_0%,transparent_40%),#edf3f1] p-5">
      <div className="absolute inset-0 opacity-25 [background-image:linear-gradient(#94a3b8_1px,transparent_1px),linear-gradient(90deg,#94a3b8_1px,transparent_1px)] [background-size:36px_36px]" />
      <div className="relative flex h-full flex-col justify-center gap-3">
        {places.slice(0, 6).map((place, index) => (
          <a
            className="flex items-center gap-3 rounded-2xl border border-white/70 bg-white/85 p-3 shadow-sm backdrop-blur transition-transform hover:translate-x-1"
            href={amapPlaceUrl(place)}
            key={place.plan_item_id}
            rel="noreferrer noopener"
            target="_blank"
          >
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-[#0f766e] text-[10px] font-bold text-white">
              {index + 1}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs font-medium">{place.name}</span>
            <ChevronRight className="text-[#8090a0]" size={14} />
          </a>
        ))}
        {places.length === 0 && (
          <div className="text-center text-xs text-[#697586]">当前没有可显示的地点坐标。</div>
        )}
        {!configured && places.length > 0 && (
          <p className="mt-1 text-center text-[10px] leading-4 text-[#697586]">
            配置前端高德 JS Key 后显示交互地图；当前仍可点击地点打开高德。
          </p>
        )}
      </div>
    </div>
  );
}
