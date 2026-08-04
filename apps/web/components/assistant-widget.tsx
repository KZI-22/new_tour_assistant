"use client";

import { Bot, Compass, LoaderCircle, Plus, Send, Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  fetchModels,
  streamChat,
  type ModelInfo,
  type ToolCallUpdate,
  type ToolResultUpdate,
} from "@/lib/api";

type AssistantMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  tools?: Array<{ id: string; label: string; state: "running" | "success" | "failed" }>;
};

const GUIDE_MESSAGE: AssistantMessage = {
  id: "travel-assistant-guide",
  role: "assistant",
  content:
    "我是你的旅行小助手，已经阅读这份旅行攻略。你可以问我攻略中的安排、让我搜索具体景点信息或规划景点之间的路线。如果想调整行程，也可以告诉我你的想法，我会先给出修改建议供你确认。",
};

function newId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

export function AssistantWidget({
  activePlanId,
  activePlanVersion,
}: {
  activePlanId: string;
  activePlanVersion: number;
}) {
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelId, setModelId] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([GUIDE_MESSAGE]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchModels(controller.signal)
      .then((catalog) => {
        setModels(catalog.models);
        const selected =
          catalog.models.find((item) => item.id === catalog.default_model && item.available) ??
          catalog.models.find((item) => item.available);
        setModelId(selected?.id ?? "");
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(reason instanceof Error ? reason.message : "模型列表加载失败。");
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const selectedModel = useMemo(
    () => models.find((model) => model.id === modelId),
    [modelId, models],
  );
  const canSend = Boolean(input.trim() && selectedModel?.available && !loading);

  const clear = () => {
    abortRef.current?.abort();
    setConversationId(null);
    setMessages([GUIDE_MESSAGE]);
    setInput("");
    setLoading(false);
    setError(null);
  };

  const updateTool = (assistantId: string, update: ToolCallUpdate | ToolResultUpdate) => {
    setMessages((current) =>
      current.map((message) => {
        if (message.id !== assistantId) return message;
        const tools = message.tools ?? [];
        const next = {
          id: update.tool_call_id,
          label: "display_name" in update ? update.display_name : update.tool_name,
          state: ("success" in update ? (update.success ? "success" : "failed") : "running") as
            | "running"
            | "success"
            | "failed",
        };
        return {
          ...message,
          tools: tools.some((item) => item.id === next.id)
            ? tools.map((item) => (item.id === next.id ? { ...next, label: item.label } : item))
            : [...tools, next],
        };
      }),
    );
  };

  const send = async () => {
    const content = input.trim();
    if (!content || !canSend) return;
    const assistantId = newId();
    const controller = new AbortController();
    abortRef.current = controller;
    setMessages((current) => [
      ...current,
      { id: newId(), role: "user", content },
      { id: assistantId, role: "assistant", content: "", tools: [] },
    ]);
    setInput("");
    setError(null);
    setLoading(true);
    try {
      await streamChat(
        modelId,
        content,
        conversationId,
        {
          onConversation: ({ id }) => setConversationId(id),
          onToken: (delta) =>
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: message.content + delta }
                  : message,
              ),
            ),
          onToolCall: (update) => updateTool(assistantId, update),
          onToolResult: (update) => updateTool(assistantId, update),
          onXhsLoginRequired: (update) =>
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: `${update.message}\n\n登录完成后会自动继续检索。` }
                  : message,
              ),
            ),
        },
        controller.signal,
        "standard",
        activePlanId,
        activePlanVersion,
      );
    } catch (reason: unknown) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : "助手暂时无法回复。");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setLoading(false);
    }
  };

  return (
    <>
      <button
        aria-label={open ? "关闭旅行助手" : "打开旅行助手"}
        className="fixed right-4 top-4 z-[90] grid size-12 place-items-center rounded-2xl border border-white/80 bg-[#0f766e] text-white shadow-xl shadow-emerald-950/20 transition-transform hover:-translate-y-0.5 sm:right-6"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? <X size={20} /> : <Bot size={22} />}
        {!open && (
          <span className="absolute -bottom-1 -left-1 grid size-5 place-items-center rounded-full border-2 border-white bg-amber-400 text-emerald-950">
            <Compass size={11} strokeWidth={2.6} />
          </span>
        )}
      </button>

      {open && (
        <section className="fixed inset-x-3 bottom-3 top-20 z-[85] flex flex-col overflow-hidden rounded-[28px] border border-black/10 bg-white shadow-2xl shadow-black/20 sm:inset-x-auto sm:right-6 sm:w-[420px]">
          <header className="flex items-center justify-between border-b border-black/[0.06] bg-[linear-gradient(135deg,#ecfdf5,#eff6ff)] px-5 py-4">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Sparkles size={16} className="text-[#0f766e]" /> 旅行智能助手
              </div>
              <p className="mt-1 text-[11px] text-[#697586]">
                已阅读当前文本攻略 · 可查询景点与规划路线
              </p>
            </div>
            <button className="icon-button" onClick={clear} title="新对话" type="button">
              <Plus size={17} />
            </button>
          </header>

          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto bg-[#f8faf9] p-4" ref={scrollRef}>
            {messages.map((message) => (
              <article
                className={`max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-6 ${
                  message.role === "user"
                    ? "ml-auto bg-[#0f766e] text-white"
                    : "border border-black/[0.055] bg-white text-[#334155]"
                }`}
                key={message.id}
              >
                {message.tools && message.tools.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {message.tools.map((tool) => (
                      <span className="rounded-full bg-[#edf5f2] px-2 py-1 text-[10px] text-[#0f766e]" key={tool.id}>
                        {tool.state === "running" ? "查询中 · " : ""}{tool.label}
                      </span>
                    ))}
                  </div>
                )}
                {message.content ? (
                  <div className="markdown-body">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                  </div>
                ) : (
                  <span className="flex items-center gap-2 text-xs text-[#697586]">
                    <LoaderCircle className="animate-spin" size={14} /> 正在思考
                  </span>
                )}
              </article>
            ))}
          </div>

          <footer className="border-t border-black/[0.06] bg-white p-3">
            {error && <p className="mb-2 px-1 text-xs text-red-600">{error}</p>}
            <textarea
              className="min-h-20 w-full resize-none rounded-2xl border border-black/10 bg-[#f8faf9] px-4 py-3 text-sm outline-none focus:border-[#0f766e]/40"
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
              placeholder="询问攻略、景点或路线…"
              value={input}
            />
            <div className="mt-2 flex items-center justify-between gap-3">
              <span className="px-1 text-[10px] text-[#8090a0]">基于当前攻略回答</span>
              <div className="flex items-center gap-2">
                <select
                  aria-label="选择助手模型"
                  className="max-w-36 rounded-xl border border-black/10 bg-white px-2.5 py-2 text-[11px] text-[#52606d]"
                  disabled={loading}
                  onChange={(event) => setModelId(event.target.value)}
                  value={modelId}
                >
                  {models.map((model) => (
                    <option disabled={!model.available} key={model.id} value={model.id}>
                      {model.display_name}
                    </option>
                  ))}
                </select>
                <button
                  aria-label="发送"
                  className="grid size-9 place-items-center rounded-xl bg-[#0f766e] text-white disabled:opacity-35"
                  disabled={!canSend}
                  onClick={() => void send()}
                  type="button"
                >
                  <Send size={15} />
                </button>
              </div>
            </div>
          </footer>
        </section>
      )}
    </>
  );
}
