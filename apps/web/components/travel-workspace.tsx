"use client";

import {
  ArrowRight,
  CalendarDays,
  Clock3,
  Compass,
  History,
  Hotel,
  LoaderCircle,
  LogOut,
  MapPinned,
  Plane,
  Sparkles,
  TrainFront,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, type MouseEvent, useEffect, useMemo, useRef, useState } from "react";

import { AssistantWidget } from "@/components/assistant-widget";
import { useAuth } from "@/components/auth-gate";
import { TravelSearchDialog, type SearchKind } from "@/components/travel-search-dialog";
import {
  deleteTravelPlan,
  fetchModels,
  fetchTravelPlans,
  streamTravelPlan,
  type PlanningStageUpdate,
  type TravelPlanSummary,
  type TripPreference,
} from "@/lib/api";

const INTERESTS: Array<{ value: TripPreference; emoji: string }> = [
  { value: "历史文化", emoji: "🏛️" },
  { value: "博物馆展览", emoji: "🖼️" },
  { value: "自然风光", emoji: "🌿" },
  { value: "城市地标", emoji: "🏙️" },
  { value: "特色街区", emoji: "🏮" },
  { value: "摄影打卡", emoji: "📷" },
  { value: "亲子游", emoji: "👨‍👩‍👧" },
  { value: "休闲慢游", emoji: "☕" },
  { value: "夜景体验", emoji: "🌙" },
];

function tomorrow(): string {
  const value = new Date();
  value.setDate(value.getDate() + 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

export function TravelWorkspace() {
  const router = useRouter();
  const { user, signOut } = useAuth();
  const [city, setCity] = useState("");
  const [startDate, setStartDate] = useState(tomorrow);
  const [duration, setDuration] = useState(3);
  const [interests, setInterests] = useState<TripPreference[]>([]);
  const [modelId, setModelId] = useState("");
  const [plans, setPlans] = useState<TravelPlanSummary[]>([]);
  const [stages, setStages] = useState<PlanningStageUpdate[]>([]);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchKind, setSearchKind] = useState<SearchKind | null>(null);
  const [deletingPlanId, setDeletingPlanId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([fetchModels(controller.signal), fetchTravelPlans(controller.signal)])
      .then(([catalog, history]) => {
        setPlans(history);
        const selected =
          catalog.models.find((item) => item.id === catalog.default_model && item.available) ??
          catalog.models.find((item) => item.available);
        setModelId(selected?.id ?? "");
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(reason instanceof Error ? reason.message : "平台数据加载失败。");
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);

  const currentStage = useMemo(
    () => [...stages].reverse().find((stage) => stage.status === "running") ?? stages.at(-1),
    [stages],
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!modelId || !city.trim() || generating) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setGenerating(true);
    setStages([]);
    setError(null);
    try {
      await streamTravelPlan(
        modelId,
        {
          destination_city: city.trim(),
          start_date: startDate,
          duration_days: duration,
          interests,
        },
        {
          onToken: () => undefined,
          onPlanningStage: (stage) =>
            setStages((current) =>
              current.some((item) => item.stage === stage.stage)
                ? current.map((item) => (item.stage === stage.stage ? stage : item))
                : [...current, stage],
            ),
          onTravelPlanReady: ({ plan_id }) => router.push(`/plans/${plan_id}`),
        },
        controller.signal,
      );
    } catch (reason: unknown) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : "旅行规划生成失败。");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setGenerating(false);
    }
  };

  const removePlan = async (event: MouseEvent<HTMLButtonElement>, planId: string) => {
    event.preventDefault();
    event.stopPropagation();
    if (
      deletingPlanId ||
      !window.confirm("确定删除这份旅行规划吗？删除后将同时移除它的所有规划版本。")
    ) {
      return;
    }
    setDeletingPlanId(planId);
    setError(null);
    try {
      await deleteTravelPlan(planId);
      setPlans((current) => current.filter((plan) => plan.plan_id !== planId));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "旅行规划删除失败。");
    } finally {
      setDeletingPlanId(null);
    }
  };

  return (
    <main className="min-h-dvh bg-[#f5f7f5] text-[#17202a]">
      <AssistantWidget />
      <header className="border-b border-black/[0.06] bg-white/90 backdrop-blur-xl">
        <div className="mx-auto flex h-20 max-w-[1180px] items-center justify-between px-4 pr-20 sm:px-6 sm:pr-24">
          <div className="flex items-center gap-3">
            <span className="grid size-11 place-items-center rounded-2xl bg-[#0f766e] text-white shadow-lg shadow-emerald-900/15"><Compass size={21} /></span>
            <div><p className="text-base font-semibold tracking-tight">远行 · 智能旅行规划</p><p className="mt-0.5 text-[11px] text-[#8090a0]">清晰填写，准确规划</p></div>
          </div>
          <button className="flex items-center gap-2 rounded-xl px-3 py-2 text-xs text-[#697586] hover:bg-black/[0.04]" onClick={() => void signOut()} type="button"><span className="hidden sm:inline">{user.display_name || "旅行者"}</span><LogOut size={15} /></button>
        </div>
      </header>

      <section className="overflow-hidden border-b border-black/[0.05] bg-[radial-gradient(circle_at_15%_20%,rgba(52,211,153,.18),transparent_30%),radial-gradient(circle_at_85%_10%,rgba(96,165,250,.14),transparent_28%),linear-gradient(145deg,#f0fdf8,#f8fafc)]">
        <div className="mx-auto max-w-[1180px] px-4 py-12 sm:px-6 sm:py-16">
          <span className="inline-flex items-center gap-2 rounded-full border border-[#0f766e]/15 bg-white/70 px-3 py-1.5 text-xs font-medium text-[#0f766e]"><Sparkles size={13} />结构化智能规划</span>
          <h1 className="mt-5 max-w-3xl text-4xl font-semibold tracking-[-0.05em] sm:text-6xl">把旅途想清楚，<br /><span className="text-[#0f766e]">剩下的交给我们。</span></h1>
          <p className="mt-5 max-w-2xl text-sm leading-7 text-[#52606d] sm:text-base">填写城市、日期、天数和兴趣，平台将基于地图、天气和真实 POI 生成干净的每日行程，并补充最多三家城市餐饮推荐。</p>
        </div>
      </section>

      <div className="mx-auto grid max-w-[1180px] gap-7 px-4 py-8 sm:px-6 lg:grid-cols-[minmax(0,1fr)_340px] lg:items-start">
        <section className="rounded-[30px] border border-black/[0.06] bg-white p-5 shadow-xl shadow-slate-900/[0.04] sm:p-8">
          <div className="flex items-center gap-3"><span className="grid size-9 place-items-center rounded-xl bg-[#e8f5ef] text-[#0f766e]"><MapPinned size={18} /></span><div><h2 className="font-semibold">创建旅游规划</h2><p className="mt-0.5 text-xs text-[#8090a0]">单城市 · 1–10 天 · 偏好可选</p></div></div>
          <form className="mt-7 grid gap-5 sm:grid-cols-2" onSubmit={(event) => void submit(event)}>
            <label className="grid gap-2 text-xs font-medium text-[#52606d] sm:col-span-2">目标城市<input className="rounded-2xl border border-black/10 px-4 py-3.5 text-base outline-none focus:border-[#0f766e]/45" maxLength={50} onChange={(event) => setCity(event.target.value)} placeholder="例如：杭州" required value={city} /></label>
            <label className="grid gap-2 text-xs font-medium text-[#52606d]">出行日期<span className="relative"><CalendarDays className="pointer-events-none absolute left-3.5 top-3.5 text-[#8090a0]" size={16} /><input className="w-full rounded-2xl border border-black/10 py-3.5 pl-11 pr-4 text-sm outline-none focus:border-[#0f766e]/45" min={tomorrow()} onChange={(event) => setStartDate(event.target.value)} required type="date" value={startDate} /></span></label>
            <label className="grid gap-2 text-xs font-medium text-[#52606d]">游玩天数<span className="relative"><Clock3 className="pointer-events-none absolute left-3.5 top-3.5 text-[#8090a0]" size={16} /><select className="w-full appearance-none rounded-2xl border border-black/10 bg-white py-3.5 pl-11 pr-4 text-sm outline-none focus:border-[#0f766e]/45" onChange={(event) => setDuration(Number(event.target.value))} value={duration}>{Array.from({ length: 10 }, (_, index) => index + 1).map((day) => <option key={day} value={day}>{day} 天</option>)}</select></span></label>
            <fieldset className="sm:col-span-2"><legend className="text-xs font-medium text-[#52606d]">旅行偏好 <span className="font-normal text-[#94a3b8]">（可多选）</span></legend><div className="mt-3 flex flex-wrap gap-2">{INTERESTS.map((item) => { const selected = interests.includes(item.value); return <button aria-pressed={selected} className={`rounded-xl border px-3 py-2 text-xs transition-colors ${selected ? "border-[#0f766e] bg-[#ecfdf5] text-[#0f766e]" : "border-black/[0.08] bg-[#fafbfb] text-[#52606d] hover:bg-[#f1f7f4]"}`} key={item.value} onClick={() => setInterests((current) => selected ? current.filter((value) => value !== item.value) : [...current, item.value])} type="button"><span className="mr-1.5">{item.emoji}</span>{item.value}</button>; })}</div></fieldset>
            <div className="sm:col-span-2"><button className="flex w-full items-center justify-center gap-2 rounded-2xl bg-[#0f766e] px-5 py-4 text-sm font-semibold text-white shadow-lg shadow-emerald-900/15 transition-transform hover:-translate-y-0.5 disabled:opacity-45" disabled={generating || !modelId} type="submit">{generating ? <LoaderCircle className="animate-spin" size={18} /> : <Sparkles size={18} />}{generating ? currentStage?.display_name || "正在生成旅行规划" : "生成我的旅行规划"}</button>{error && <p className="mt-3 rounded-2xl bg-red-50 p-3 text-sm text-red-700">{error}</p>}</div>
          </form>
          {generating && stages.length > 0 && <div className="mt-5 grid gap-2 sm:grid-cols-3">{stages.map((stage) => <div className="rounded-xl bg-[#f5f8f7] px-3 py-2.5 text-[11px] text-[#52606d]" key={stage.stage}><span className={`mr-2 inline-block size-1.5 rounded-full ${stage.status === "running" ? "animate-pulse bg-amber-400" : stage.status === "failed" ? "bg-red-400" : "bg-emerald-500"}`} />{stage.display_name}</div>)}</div>}
        </section>

        <aside className="space-y-5">
          <section className="rounded-[26px] border border-black/[0.06] bg-white p-5"><div className="flex items-center gap-2 text-sm font-semibold"><Sparkles size={16} className="text-amber-500" />按需查询</div><p className="mt-2 text-xs leading-5 text-[#8090a0]">不会自动带入规划信息，请按实际需求自行填写。</p><div className="mt-4 grid gap-2">{([{ kind: "hotel", label: "酒店", icon: Hotel }, { kind: "flight", label: "航班", icon: Plane }, { kind: "train", label: "火车", icon: TrainFront }] as const).map(({ kind, label, icon: Icon }) => <button className="flex items-center justify-between rounded-2xl border border-black/[0.07] bg-[#fbfcfc] px-4 py-3 text-sm font-medium hover:bg-[#f2f8f5]" key={kind} onClick={() => setSearchKind(kind)} type="button"><span className="flex items-center gap-2.5"><Icon size={16} className="text-[#0f766e]" />查询{label}</span><ArrowRight size={14} className="text-[#94a3b8]" /></button>)}</div></section>
          <section className="rounded-[26px] border border-black/[0.06] bg-white p-5"><div className="flex items-center gap-2 text-sm font-semibold"><History size={16} className="text-[#0f766e]" />最近规划</div><div className="mt-4 space-y-2">{plans.length === 0 ? <p className="text-xs leading-5 text-[#8090a0]">生成后的旅行规划会保存在这里。</p> : plans.slice(0, 6).map((plan) => <div className="flex items-center gap-2 rounded-2xl bg-[#f7f9f8] p-3.5 transition-colors hover:bg-[#eef6f2]" key={plan.plan_id}><Link className="min-w-0 flex-1" href={`/plans/${plan.plan_id}`}><p className="truncate text-sm font-medium">{plan.title}</p><p className="mt-1 text-[11px] text-[#8090a0]">{plan.start_date} · {plan.duration_days} 天 · v{plan.current_version}</p></Link><button aria-label={`删除旅行规划：${plan.title}`} className="grid size-8 shrink-0 place-items-center rounded-xl text-[#94a3b8] transition-colors hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40" disabled={deletingPlanId === plan.plan_id} onClick={(event) => void removePlan(event, plan.plan_id)} title="删除旅行规划" type="button">{deletingPlanId === plan.plan_id ? <LoaderCircle className="animate-spin" size={14} /> : <Trash2 size={14} />}</button></div>)}</div></section>
        </aside>
      </div>
      <TravelSearchDialog
        key={searchKind ?? "closed"}
        kind={searchKind}
        onClose={() => setSearchKind(null)}
      />
    </main>
  );
}
