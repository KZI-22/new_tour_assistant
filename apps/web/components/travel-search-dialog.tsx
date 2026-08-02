"use client";

import {
  BedDouble,
  CalendarDays,
  ExternalLink,
  Hotel,
  LoaderCircle,
  Plane,
  Search,
  TrainFront,
  X,
} from "lucide-react";
import { FormEvent, useState } from "react";

import {
  searchFlights,
  searchHotels,
  searchTrains,
  type DirectTravelSearchResponse,
  type HotelOption,
  type TransportOption,
} from "@/lib/api";

export type SearchKind = "hotel" | "flight" | "train";

const SEARCH_META = {
  hotel: { title: "查询酒店", icon: Hotel, hint: "最多展示 10 家酒店" },
  flight: { title: "查询航班", icon: Plane, hint: "最多展示 5 个航班" },
  train: { title: "查询火车", icon: TrainFront, hint: "最多展示 5 个班次" },
} as const;

function safeUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

function price(value: string | number | null): string | null {
  if (value === null || value === "") return null;
  return `¥${value}`;
}

export function TravelSearchDialog({ kind, onClose }: { kind: SearchKind | null; onClose: () => void }) {
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [keywords, setKeywords] = useState("");
  const [nearbyPoi, setNearbyPoi] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [stars, setStars] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DirectTravelSearchResponse | null>(null);

  if (!kind) return null;
  const meta = SEARCH_META[kind];
  const Icon = meta.icon;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const parsedPrice = maxPrice ? Number(maxPrice) : undefined;
      const response =
        kind === "hotel"
          ? await searchHotels({
              destination,
              check_in_date: startDate,
              check_out_date: endDate,
              ...(keywords ? { keywords } : {}),
              ...(nearbyPoi ? { nearby_poi: nearbyPoi } : {}),
              ...(stars.length ? { hotel_stars: stars } : {}),
              ...(parsedPrice ? { max_price: parsedPrice } : {}),
            })
          : kind === "flight"
            ? await searchFlights({
                origin,
                destination,
                departure_date: startDate,
                ...(parsedPrice ? { max_price: parsedPrice } : {}),
              })
            : await searchTrains({
                origin,
                destination,
                departure_date: startDate,
                ...(parsedPrice ? { max_price: parsedPrice } : {}),
              });
      setResult(response);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "查询失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[100] grid place-items-center bg-slate-950/40 p-3 backdrop-blur-sm" role="dialog">
      <button aria-label="关闭查询窗口" className="absolute inset-0" onClick={onClose} type="button" />
      <section className="relative z-10 flex max-h-[92dvh] w-full max-w-3xl flex-col overflow-hidden rounded-[30px] bg-white shadow-2xl">
        <header className="flex items-center justify-between border-b border-black/[0.06] bg-[linear-gradient(135deg,#effcf7,#f5f8ff)] px-5 py-4 sm:px-7">
          <div className="flex items-center gap-3">
            <span className="grid size-10 place-items-center rounded-2xl bg-[#0f766e] text-white"><Icon size={19} /></span>
            <div>
              <h2 className="font-semibold">{meta.title}</h2>
              <p className="mt-0.5 text-[11px] text-[#697586]">请自行填写查询条件 · {meta.hint}</p>
            </div>
          </div>
          <button className="icon-button" onClick={onClose} type="button"><X size={18} /></button>
        </header>

        <div className="min-h-0 overflow-y-auto p-5 sm:p-7">
          <form className="grid gap-4 sm:grid-cols-2" onSubmit={(event) => void submit(event)}>
            {kind !== "hotel" && (
              <Field label="出发地"><input required value={origin} onChange={(event) => setOrigin(event.target.value)} placeholder="例如：上海" /></Field>
            )}
            <Field label={kind === "hotel" ? "目的地" : "到达地"}><input required value={destination} onChange={(event) => setDestination(event.target.value)} placeholder="请填写城市、车站或机场" /></Field>
            <Field label={kind === "hotel" ? "入住日期" : "出发日期"}><input required type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></Field>
            {kind === "hotel" && (
              <Field label="退房日期"><input required type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></Field>
            )}
            {kind === "hotel" && (
              <>
                <Field label="关键词（可选）"><input value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="例如：亲子、温泉" /></Field>
                <Field label="附近地点（可选）"><input value={nearbyPoi} onChange={(event) => setNearbyPoi(event.target.value)} placeholder="例如：西湖" /></Field>
                <div className="sm:col-span-2">
                  <span className="text-xs font-medium text-[#52606d]">酒店星级（可选）</span>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {[1, 2, 3, 4, 5].map((star) => (
                      <button className={`rounded-xl border px-3 py-2 text-xs ${stars.includes(star) ? "border-[#0f766e] bg-[#ecfdf5] text-[#0f766e]" : "border-black/10 text-[#697586]"}`} key={star} onClick={() => setStars((current) => current.includes(star) ? current.filter((item) => item !== star) : [...current, star])} type="button">{star} 星</button>
                    ))}
                  </div>
                </div>
              </>
            )}
            <Field label={kind === "hotel" ? "每晚最高价（可选）" : "最高总价（可选）"}><input min="1" type="number" value={maxPrice} onChange={(event) => setMaxPrice(event.target.value)} placeholder="人民币" /></Field>
            <div className="flex items-end">
              <button className="flex w-full items-center justify-center gap-2 rounded-2xl bg-[#0f766e] px-5 py-3 text-sm font-semibold text-white disabled:opacity-45" disabled={loading} type="submit">
                {loading ? <LoaderCircle className="animate-spin" size={17} /> : <Search size={17} />}{loading ? "查询中" : "开始查询"}
              </button>
            </div>
          </form>

          {error && <p className="mt-5 rounded-2xl bg-red-50 p-4 text-sm text-red-700">{error}</p>}
          {result && (
            <section className="mt-7 border-t border-black/[0.06] pt-6">
              <p className={`rounded-2xl p-4 text-sm leading-6 ${result.success ? "bg-[#effaf6] text-[#28594b]" : "bg-amber-50 text-amber-900"}`}>{result.summary}</p>
              <div className="mt-4 grid gap-3 sm:grid-cols-2">
                {result.options.map((option) =>
                  option.kind === "hotel" ? <HotelResult key={option.option_id} option={option} /> : <TransportResult key={option.option_id} option={option} />,
                )}
              </div>
            </section>
          )}
        </div>
      </section>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="grid gap-2 text-xs font-medium text-[#52606d]">{label}<span className="[&_input]:w-full [&_input]:rounded-xl [&_input]:border [&_input]:border-black/10 [&_input]:bg-white [&_input]:px-3.5 [&_input]:py-3 [&_input]:text-sm [&_input]:text-[#17202a] [&_input]:outline-none [&_input]:focus:border-[#0f766e]/45">{children}</span></label>;
}

function DetailLink({ href }: { href: string | null }) {
  const url = safeUrl(href);
  return url ? <a className="mt-3 inline-flex items-center gap-1.5 rounded-xl bg-[#ff6a00] px-3 py-2 text-xs font-semibold text-white" href={url} rel="noreferrer noopener" target="_blank">查看详情 <ExternalLink size={12} /></a> : null;
}

function HotelResult({ option }: { option: HotelOption }) {
  return <article className="rounded-2xl border border-black/[0.07] bg-[#fbfcfc] p-4"><div className="flex items-start justify-between gap-3"><div><h3 className="flex items-center gap-2 text-sm font-semibold"><BedDouble size={15} className="text-amber-600" />{option.name}</h3><p className="mt-2 text-xs leading-5 text-[#697586]">{[option.star, option.nearby_poi, option.address].filter(Boolean).join(" · ") || "酒店候选"}</p></div>{price(option.price_amount) && <span className="text-sm font-semibold text-orange-600">{price(option.price_amount)}</span>}</div><DetailLink href={option.detail_url} /></article>;
}

function TransportResult({ option }: { option: TransportOption }) {
  return <article className="rounded-2xl border border-black/[0.07] bg-[#fbfcfc] p-4"><div className="flex items-start justify-between gap-3"><div><h3 className="text-sm font-semibold">{[...option.transport_names, ...option.transport_numbers].filter(Boolean).join(" · ") || "出行候选"}</h3><p className="mt-2 flex items-center gap-1.5 text-xs text-[#697586]"><CalendarDays size={13} />{option.departure_at}</p><p className="mt-1 text-xs text-[#52606d]">{option.departure_station} → {option.arrival_station}</p></div>{price(option.price_amount) && <span className="text-sm font-semibold text-orange-600">{price(option.price_amount)}</span>}</div><DetailLink href={option.detail_url} /></article>;
}
