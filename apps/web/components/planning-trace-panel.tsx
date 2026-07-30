"use client";

import {
  Bug,
  Check,
  ChevronDown,
  Clock3,
  LoaderCircle,
  Search,
  SkipForward,
  TriangleAlert,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

import { AgentTraceUpdate, PlanningTraceUpdate } from "@/lib/api";

type TraceUpdate = PlanningTraceUpdate | AgentTraceUpdate;

type TracePost = {
  search_rank?: number;
  reference_id?: string;
  role?: string;
  note_id?: string;
  title?: string;
  author_name?: string;
  liked_count_raw?: string | null;
  liked_count?: number | null;
  selection_status?: string;
  reason?: string;
  content_chars?: number;
  image_count?: number;
};

const hiddenKeys = new Set(["posts", "content_preview", "latest_user_message"]);

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function posts(value: unknown): TracePost[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => record(item) as TracePost);
}

function labelForKey(key: string): string {
  const labels: Record<string, string> = {
    keyword: "搜索关键词",
    sort_by: "排序方式",
    result_scope: "结果范围",
    total_count: "搜索结果",
    candidate_count: "详情候选",
    candidate_limit: "候选上限",
    destination_city: "目标城市",
    duration_days: "游玩天数",
    extraction_method: "提取方式",
    explicit_duration_override: "显式天数覆盖",
    is_logged_in: "登录状态",
    evidence_count: "证据数量",
    evidence_chars: "证据字数",
    conversation_message_count: "上下文消息数",
    day_count: "生成天数",
    activity_count: "活动数量",
    source_count: "引用来源",
    warning_count: "警告数量",
    output_chars: "输出字数",
    route: "处理流程",
    route_source: "路由来源",
    result: "处理结果",
    error_code: "错误代码",
    search_rank: "搜索排名",
    content_chars: "正文字数",
    image_count: "图片数量",
    rejected_image_count: "跳过图片",
    upgraded_image_count: "HTTPS 升级图片",
    source_image_count: "来源图片",
    minimum_content_chars: "最低正文要求",
    reference_id: "来源编号",
    allowed_source_refs: "允许的来源",
    source_refs: "实际来源",
  };
  return labels[key] ?? key;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length ? value.join("、") : "无";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function statusIcon(status: TraceUpdate["status"]) {
  if (status === "running") return <LoaderCircle size={13} className="animate-spin" />;
  if (status === "failed") return <X size={13} />;
  if (status === "partial") return <TriangleAlert size={13} />;
  if (status === "skipped") return <SkipForward size={13} />;
  return <Check size={13} />;
}

function postStatus(post: TracePost): { label: string; className: string } {
  if (post.reference_id) {
    return { label: `最终采用 · ${post.reference_id}`, className: "text-emerald-700" };
  }
  if (post.selection_status === "candidate") {
    return { label: "进入详情候选", className: "text-blue-700" };
  }
  return { label: post.reason ?? "未采用", className: "text-[var(--muted)]" };
}

function TraceData({ trace }: { trace: TraceUpdate }) {
  const data = trace.data;
  const tracePosts = posts(data.posts);
  const preview = text(data.content_preview);
  const userInput = text(data.latest_user_message);
  const visibleEntries = Object.entries(data).filter(([key]) => !hiddenKeys.has(key));

  return (
    <div className="mt-2 space-y-2">
      {userInput && (
        <div className="rounded-lg bg-black/[0.035] px-2.5 py-2 text-xs leading-5 text-[var(--ink)]">
          {userInput}
        </div>
      )}
      {visibleEntries.length > 0 && (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2">
          {visibleEntries.map(([key, value]) => (
            <div key={key} className="flex min-w-0 gap-2">
              <dt className="shrink-0 text-[var(--muted-light)]">{labelForKey(key)}</dt>
              <dd className="min-w-0 break-words text-[var(--ink)]">{formatValue(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {tracePosts.length > 0 && (
        <div className="max-h-80 space-y-1.5 overflow-y-auto pr-1">
          {tracePosts.map((post, index) => {
            const state = postStatus(post);
            return (
              <div
                key={`${post.note_id ?? "post"}-${post.search_rank ?? index}`}
                className="rounded-lg border border-black/[0.055] bg-white/70 px-2.5 py-2"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-xs font-medium text-[var(--ink)]">
                      {post.search_rank ? `#${post.search_rank} ` : ""}
                      {post.title || "未命名笔记"}
                    </p>
                    <p className="mt-0.5 truncate text-[10px] text-[var(--muted-light)]">
                      {post.author_name || "未知作者"}
                      {post.liked_count_raw ? ` · 点赞 ${post.liked_count_raw}` : ""}
                      {post.content_chars !== undefined ? ` · ${post.content_chars} 字` : ""}
                      {post.image_count !== undefined ? ` · ${post.image_count} 图` : ""}
                    </p>
                  </div>
                  <span className={`shrink-0 text-[10px] ${state.className}`}>{state.label}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {preview && (
        <div className="rounded-lg border-l-2 border-[var(--brand)] bg-black/[0.025] px-2.5 py-2 text-[11px] leading-5 text-[var(--muted)]">
          {preview}
          {preview.length >= 240 ? "…" : ""}
        </div>
      )}
    </div>
  );
}

function traceStep(trace: TraceUpdate): string {
  return trace.type === "planning_trace" ? trace.step : trace.action;
}

export function PlanningTracePanel({ traces }: { traces: TraceUpdate[] }) {
  const [open, setOpen] = useState(false);
  const ordered = useMemo(
    () => [...traces].sort((left, right) => left.sequence - right.sequence),
    [traces],
  );
  const searchTrace = [...ordered]
    .reverse()
    .find(
      (trace) =>
        trace.type === "planning_trace" &&
        trace.step === "search_results" &&
        number(trace.data.total_count) !== null,
    );
  const totalPosts = number(searchTrace?.data.total_count) ?? 0;

  if (ordered.length === 0) return null;

  return (
    <div className="mb-3 overflow-hidden rounded-xl border border-black/[0.07] bg-white/55">
      <button
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="flex min-w-0 items-center gap-2 text-xs font-medium">
          <Bug size={14} className="text-[var(--brand)]" />
          Agent 调试视图
          <span className="font-normal text-[var(--muted-light)]">
            {ordered.length} 个事件{totalPosts ? ` · ${totalPosts} 篇搜索结果` : ""}
          </span>
        </span>
        <ChevronDown
          size={15}
          className={`shrink-0 text-[var(--muted)] transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && (
        <div className="border-t border-black/[0.06] px-3 pb-3 pt-2.5">
          <div className="mb-2 flex items-center gap-2 text-[10px] text-[var(--muted-light)]">
            <Search size={11} />
            已脱敏，不包含 xsec_token、Cookie、登录 ID 或鉴权信息
          </div>
          <div className="space-y-2.5">
            {ordered.map((trace) => (
              <section
                key={`${trace.sequence}-${traceStep(trace)}`}
                className="rounded-lg border border-black/[0.05] bg-black/[0.018] px-2.5 py-2"
              >
                <div className="flex items-start justify-between gap-3">
                  <div
                    className={`flex min-w-0 items-center gap-1.5 text-xs ${
                      trace.status === "failed"
                        ? "text-red-700"
                        : trace.status === "partial"
                          ? "text-amber-700"
                          : trace.status === "skipped"
                            ? "text-[var(--muted-light)]"
                            : "text-[var(--ink)]"
                    }`}
                  >
                    {statusIcon(trace.status)}
                    <span className="font-medium">{trace.title}</span>
                  </div>
                  {trace.duration_ms !== null && (
                    <span className="flex shrink-0 items-center gap-1 text-[10px] text-[var(--muted-light)]">
                      <Clock3 size={10} />
                      {trace.duration_ms} ms
                    </span>
                  )}
                </div>
                {trace.detail && <p className="mt-1 text-[11px] text-[var(--muted)]">{trace.detail}</p>}
                <TraceData trace={trace} />
              </section>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
