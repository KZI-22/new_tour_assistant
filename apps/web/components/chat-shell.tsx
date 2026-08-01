"use client";

import {
  ArrowUp,
  Bot,
  BookOpenText,
  Check,
  ChevronDown,
  CircleStop,
  Compass,
  LoaderCircle,
  LogOut,
  Menu,
  MapPinned,
  MessageSquareText,
  Plus,
  Sparkles,
  TriangleAlert,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import Link from "next/link";
import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AssistantMarkdown } from "@/components/assistant-markdown";
import { useAuth } from "@/components/auth-gate";
import { PlanningTracePanel } from "@/components/planning-trace-panel";
import {
  ApiChatMessage,
  ConversationSummary,
  deleteConversation,
  fetchConversation,
  fetchConversations,
  fetchModels,
  ModelInfo,
  PlanningSource,
  streamChat,
  ToolCallUpdate,
  ToolResultUpdate,
  PlanningStageUpdate,
  PlanningTraceUpdate,
  TravelPlanReference,
  XhsLoginRequiredUpdate,
} from "@/lib/api";

type ToolStatus = {
  id: string;
  toolName: string;
  label: string;
  state: "running" | "success" | "partial" | "failed";
  summary?: string;
};

type ChatMessage = ApiChatMessage & {
  tools?: ToolStatus[];
  planningStages?: PlanningStageUpdate[];
  debugTrace?: PlanningTraceUpdate[];
  travelPlan?: TravelPlanReference;
  xhsLogin?: XhsLoginRequiredUpdate & { status: "pending" | "failed" | "skipped" };
};

type SendMessageOptions = {
  planningSource?: PlanningSource;
  allowWhileLoading?: boolean;
};

const MAP_FALLBACK_MESSAGE = "跳过小红书登录，使用地图与天气继续生成。";

const suggestions = [
  "帮我规划一趟明天开始的 5 天东京美食之旅",
  "搜索成都 3 天游的小红书高赞原帖",
  "设计一个明天开始、适合亲子的上海 2 天行程",
  "如何控制欧洲自由行的整体预算？",
];

function newId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function maskedPhone(phone: string): string {
  const localNumber = phone.startsWith("+86") ? phone.slice(3) : phone;
  if (localNumber.length < 7) return phone;
  const prefix = phone.startsWith("+86") ? "+86 " : "";
  return `${prefix}${localNumber.slice(0, 3)}****${localNumber.slice(-4)}`;
}

export function ChatShell() {
  const { user, signOut } = useAuth();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [planningSource, setPlanningSource] = useState<PlanningSource>("standard");
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [isSigningOut, setIsSigningOut] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const fallbackStartingRef = useRef<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const refreshConversations = useCallback(async (signal?: AbortSignal) => {
    try {
      setConversations(await fetchConversations(signal));
    } catch (reason: unknown) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setError(reason instanceof Error ? reason.message : "无法加载历史会话。");
    } finally {
      setConversationsLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchModels(controller.signal)
      .then((catalog) => {
        setModels(catalog.models);
        const defaultAvailable = catalog.models.find(
          (item) => item.id === catalog.default_model && item.available,
        );
        const firstAvailable = catalog.models.find((item) => item.available);
        setSelectedModel(defaultAvailable?.id ?? firstAvailable?.id ?? catalog.default_model ?? "");
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "无法加载模型列表。");
      })
      .finally(() => setModelsLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchConversations(controller.signal)
      .then(setConversations)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "无法加载历史会话。");
      })
      .finally(() => setConversationsLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [input]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const currentModel = useMemo(
    () => models.find((item) => item.id === selectedModel),
    [models, selectedModel],
  );
  const canSend = Boolean(input.trim() && currentModel?.available && !isLoading);

  const clearChat = () => {
    abortRef.current?.abort();
    setConversationId(null);
    setPlanningSource("standard");
    setMessages([]);
    setInput("");
    setError(null);
    setIsLoading(false);
    setSidebarOpen(false);
  };

  const openConversation = async (id: string) => {
    if (isLoading || id === conversationId) {
      setSidebarOpen(false);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setIsLoading(true);
    setError(null);
    try {
      const conversation = await fetchConversation(id, controller.signal);
      setConversationId(conversation.id);
      setSelectedModel(conversation.model_id);
      setPlanningSource(conversation.planning_source);
      setMessages(
        conversation.messages
          .filter(
            (message) =>
              message.status === "completed" ||
              Boolean(message.content && message.status === "interrupted"),
          )
          .map((message) => {
            const trace = message.debug_trace ?? [];
            return {
              id: message.id,
              role: message.role,
              content: message.content,
              debugTrace: trace,
              travelPlan: message.travel_plan ?? undefined,
              tools: (conversation.tool_calls ?? [])
                .filter((tool) => tool.assistant_message_id === message.id)
                .map((tool) => ({
                  id: tool.tool_call_id,
                  toolName: tool.tool_name,
                  label: tool.tool_name,
                  state:
                    tool.data_status === "partial"
                      ? "partial"
                      : tool.data_status === "usable" ||
                          (tool.data_status === null && tool.status === "success")
                        ? "success"
                        : "failed",
                  summary: tool.result_summary,
                })),
            };
          }),
      );
      setSidebarOpen(false);
    } catch (reason: unknown) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : "无法加载会话。");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setIsLoading(false);
    }
  };

  const removeConversation = async (id: string) => {
    try {
      await deleteConversation(id);
      setConversations((current) => current.filter((conversation) => conversation.id !== id));
      if (id === conversationId) clearChat();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "删除会话失败。");
    }
  };

  const stopGeneration = () => {
    abortRef.current?.abort();
  };

  const logOut = async () => {
    abortRef.current?.abort();
    setIsSigningOut(true);
    setError(null);
    try {
      await signOut();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "退出登录失败，请稍后重试。");
      setIsSigningOut(false);
    }
  };

  const sendMessage = async (content = input, options: SendMessageOptions = {}) => {
    const trimmed = content.trim();
    if (!trimmed || !currentModel?.available || (isLoading && !options.allowWhileLoading)) return;

    const userMessage: ChatMessage = { id: newId(), role: "user", content: trimmed };
    const assistantId = newId();
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      tools: [],
    };
    const requestMessages = [...messages, userMessage];
    const controller = new AbortController();

    abortRef.current = controller;
    setInput("");
    setError(null);
    setIsLoading(true);
    setMessages([...requestMessages, assistantMessage]);

    try {
      await streamChat(
        selectedModel,
        trimmed,
        conversationId,
        {
          onConversation: ({ id, planning_source }) => {
            setConversationId(id);
            setPlanningSource(planning_source);
            void refreshConversations();
          },
          onToken: (delta) => {
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, content: message.content + delta }
                  : message,
              ),
            );
          },
          onToolCall: (update: ToolCallUpdate) => {
            setMessages((current) =>
              current.map((message) => {
                if (message.id !== assistantId) return message;
                const tools = message.tools ?? [];
                const nextStatus: ToolStatus = {
                  id: update.tool_call_id,
                  toolName: update.tool_name,
                  label: update.display_name,
                  state: "running",
                };
                return {
                  ...message,
                  tools: tools.some((tool) => tool.id === update.tool_call_id)
                    ? tools.map((tool) => (tool.id === update.tool_call_id ? nextStatus : tool))
                    : [...tools, nextStatus],
                };
              }),
            );
          },
          onToolResult: (update: ToolResultUpdate) => {
            setMessages((current) =>
              current.map((message) => {
                if (message.id !== assistantId) return message;
                const tools = message.tools ?? [];
                const existing = tools.find((tool) => tool.id === update.tool_call_id);
                const nextStatus: ToolStatus = {
                  id: update.tool_call_id,
                  toolName: update.tool_name,
                  label: existing?.label ?? update.tool_name,
                  state:
                    update.data_status === "partial"
                      ? "partial"
                      : update.success
                        ? "success"
                        : "failed",
                  summary: update.summary,
                };
                return {
                  ...message,
                  tools: existing
                    ? tools.map((tool) => (tool.id === update.tool_call_id ? nextStatus : tool))
                    : [...tools, nextStatus],
                };
              }),
            );
          },
          onPlanningStage: (update: PlanningStageUpdate) => {
            setMessages((current) =>
              current.map((message) => {
                if (message.id !== assistantId) return message;
                const stages = message.planningStages ?? [];
                const xhsLogin =
                  update.stage === "waiting_xhs_login" && update.status === "success"
                    ? undefined
                    : update.stage === "waiting_xhs_login" &&
                        update.status === "failed" &&
                        message.xhsLogin
                      ? {
                          ...message.xhsLogin,
                          status: "failed" as const,
                          message: update.detail ?? "小红书登录未完成，请重新发起规划。",
                        }
                      : message.xhsLogin;
                return {
                  ...message,
                  xhsLogin,
                  planningStages: stages.some((stage) => stage.stage === update.stage)
                    ? stages.map((stage) => (stage.stage === update.stage ? update : stage))
                    : [...stages, update],
                };
              }),
            );
          },
          onPlanningTrace: (update: PlanningTraceUpdate) => {
            setMessages((current) =>
              current.map((message) => {
                if (message.id !== assistantId) return message;
                const traces = message.debugTrace ?? [];
                return {
                  ...message,
                  debugTrace: traces.some((trace) => trace.sequence === update.sequence)
                    ? traces.map((trace) =>
                        trace.sequence === update.sequence ? update : trace,
                      )
                    : [...traces, update],
                };
              }),
            );
          },
          onXhsLoginRequired: (update: XhsLoginRequiredUpdate) => {
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId
                  ? { ...message, xhsLogin: { ...update, status: "pending" } }
                  : message,
              ),
            );
          },
          onTravelPlanReady: (update: TravelPlanReference) => {
            setMessages((current) =>
              current.map((message) =>
                message.id === assistantId ? { ...message, travelPlan: update } : message,
              ),
            );
          },
          onDone: () => void refreshConversations(),
        },
        controller.signal,
        options.planningSource ?? planningSource,
      );
    } catch (reason: unknown) {
      const aborted = reason instanceof DOMException && reason.name === "AbortError";
      if (!aborted) {
        setError(reason instanceof Error ? reason.message : "消息发送失败，请稍后重试。");
      }
      void refreshConversations();
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId && !message.content
            ? { ...message, content: aborted ? "已停止生成。" : "抱歉，这次没有成功获得回复。" }
            : message,
        ),
      );
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setIsLoading(false);
      }
    }
  };

  const skipXhsLogin = async (assistantId: string) => {
    if (fallbackStartingRef.current.has(assistantId) || !currentModel?.available) return;
    fallbackStartingRef.current.add(assistantId);
    abortRef.current?.abort();
    setPlanningSource("standard");
    const fallbackRequest = sendMessage(MAP_FALLBACK_MESSAGE, {
      planningSource: "standard",
      allowWhileLoading: true,
    });
    setMessages((current) =>
      current.map((message) =>
        message.id === assistantId && message.xhsLogin
          ? {
              ...message,
              xhsLogin: {
                ...message.xhsLogin,
                status: "skipped" as const,
                message: "已跳过小红书登录，正在启动地图与天气方案。",
              },
            }
          : message,
      ),
    );
    try {
      await fallbackRequest;
    } finally {
      fallbackStartingRef.current.delete(assistantId);
    }
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (canSend) void sendMessage();
    }
  };

  return (
    <main className="flex h-dvh min-h-[620px] overflow-hidden bg-[var(--canvas)] text-[var(--ink)]">
      {sidebarOpen && (
        <button
          aria-label="关闭侧栏"
          className="fixed inset-0 z-30 bg-black/20 backdrop-blur-[1px] md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[280px] flex-col border-r border-black/[0.06] bg-[var(--sidebar)] p-3 transition-transform duration-200 md:static md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-12 items-center justify-between px-2">
          <div className="flex items-center gap-2.5 font-semibold tracking-tight">
            <span className="grid size-8 place-items-center rounded-xl bg-[var(--brand)] text-white shadow-sm">
              <Compass size={18} strokeWidth={2.2} />
            </span>
            <span>远行</span>
          </div>
          <button
            aria-label="关闭侧栏"
            className="icon-button md:hidden"
            onClick={() => setSidebarOpen(false)}
          >
            <X size={18} />
          </button>
        </div>

        <button className="new-chat-button" onClick={clearChat}>
          <Plus size={17} />
          新对话
        </button>

        <div className="mt-6 px-2 text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--muted)]">
          对话
        </div>
        <div className="mt-2 min-h-0 flex-1 space-y-1 overflow-y-auto">
          {conversationsLoading ? (
            <div className="flex items-center gap-2 px-3 py-3 text-xs text-[var(--muted)]">
              <LoaderCircle size={14} className="animate-spin" />
              正在加载历史会话
            </div>
          ) : conversations.length === 0 ? (
            <p className="px-3 py-3 text-xs leading-5 text-[var(--muted)]">还没有保存的会话。</p>
          ) : (
            conversations.map((conversation) => (
              <div
                key={conversation.id}
                className={`flex w-full items-center rounded-xl transition-colors hover:bg-black/[0.04] ${
                  conversation.id === conversationId ? "bg-black/[0.055]" : ""
                }`}
              >
                <button
                  className="flex min-w-0 flex-1 items-center gap-2.5 px-3 py-2.5 text-left text-sm"
                  disabled={isLoading}
                  onClick={() => void openConversation(conversation.id)}
                  title={conversation.title}
                >
                  <MessageSquareText size={16} className="shrink-0 text-[var(--muted)]" />
                  <span className="truncate">{conversation.title}</span>
                </button>
                <button
                  aria-label={`删除会话：${conversation.title}`}
                  className="mr-2 grid size-7 shrink-0 place-items-center rounded-lg text-[var(--muted)] hover:bg-black/[0.06] hover:text-[var(--ink)] disabled:opacity-40"
                  disabled={isLoading}
                  onClick={() => void removeConversation(conversation.id)}
                  title="删除会话"
                >
                  <X size={14} />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="mt-auto flex items-center gap-3 rounded-2xl border border-black/[0.06] bg-white/70 p-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-[var(--brand-soft)] text-[var(--brand)]">
            <UserRound size={17} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-medium">{user.display_name || "旅行者"}</p>
            <p className="mt-0.5 truncate text-[11px] text-[var(--muted)]">
              {maskedPhone(user.phone)}
            </p>
          </div>
          <button
            aria-label="退出登录"
            className="grid size-8 shrink-0 place-items-center rounded-lg text-[var(--muted)] transition-colors hover:bg-black/[0.05] hover:text-[var(--ink)] disabled:opacity-40"
            disabled={isSigningOut}
            onClick={() => void logOut()}
            title="退出登录"
          >
            {isSigningOut ? <LoaderCircle className="animate-spin" size={15} /> : <LogOut size={15} />}
          </button>
        </div>

        <div className="mt-2 rounded-2xl border border-black/[0.06] bg-white/60 p-3.5">
          <div className="flex items-center gap-2 text-xs font-medium">
            <Sparkles size={14} className="text-[var(--brand)]" />
            当前阶段
          </div>
          <p className="mt-2 text-xs leading-5 text-[var(--muted)]">
            城市行程默认使用地图与天气；开启“小红书原帖”后，消息会直接用于搜索并返回原帖正文。
          </p>
        </div>
      </aside>

      <section className="relative flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 shrink-0 items-center justify-between border-b border-black/[0.055] bg-[var(--canvas)]/90 px-4 backdrop-blur md:px-6">
          <div className="flex items-center gap-2">
            <button
              aria-label="打开侧栏"
              className="icon-button md:hidden"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={19} />
            </button>
            <div className="relative">
              <button
                className="model-trigger"
                disabled={modelsLoading}
                onClick={() => setModelMenuOpen((open) => !open)}
              >
                {modelsLoading ? "正在加载模型" : currentModel?.display_name || "选择模型"}
                {modelsLoading ? (
                  <LoaderCircle size={14} className="animate-spin" />
                ) : (
                  <ChevronDown size={14} />
                )}
              </button>
              {modelMenuOpen && (
                <>
                  <button
                    aria-label="关闭模型菜单"
                    className="fixed inset-0 z-10 cursor-default"
                    onClick={() => setModelMenuOpen(false)}
                  />
                  <div className="absolute left-0 top-[calc(100%+8px)] z-20 w-[min(340px,calc(100vw-32px))] rounded-2xl border border-black/10 bg-white p-1.5 shadow-xl shadow-black/10">
                    {models.length === 0 ? (
                      <p className="px-3 py-4 text-sm text-[var(--muted)]">配置中没有已启用的模型。</p>
                    ) : (
                      models.map((model) => (
                        <button
                          key={model.id}
                          className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-black/[0.04] disabled:cursor-not-allowed disabled:opacity-50"
                          disabled={!model.available || isLoading}
                          onClick={() => {
                            setSelectedModel(model.id);
                            setModelMenuOpen(false);
                          }}
                        >
                          <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-[var(--brand-soft)] text-[var(--brand)]">
                            <Bot size={15} />
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-2 text-sm font-medium">
                              {model.display_name}
                              {!model.available && (
                                <span className="text-[10px] font-normal text-amber-700">未配置</span>
                              )}
                            </span>
                            <span className="mt-0.5 block truncate text-xs text-[var(--muted)]">
                              {model.available ? model.description || model.provider : model.unavailable_reason}
                            </span>
                          </span>
                          {selectedModel === model.id && <Check size={15} className="mt-1 text-[var(--brand)]" />}
                        </button>
                      ))
                    )}
                  </div>
                </>
              )}
            </div>
          </div>

          <button
            aria-label={conversationId ? "删除当前会话" : "清空对话"}
            className="icon-button"
            disabled={messages.length === 0}
            onClick={() => (conversationId ? void removeConversation(conversationId) : clearChat())}
            title={conversationId ? "删除当前会话" : "清空对话"}
          >
            <Trash2 size={17} />
          </button>
        </header>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex min-h-full w-full max-w-[860px] flex-col px-4 pb-44 pt-8 md:px-8 md:pt-12">
            {messages.length === 0 ? (
              <div className="my-auto flex flex-col items-center py-12 text-center">
                <span className="grid size-14 place-items-center rounded-2xl bg-[var(--brand)] text-white shadow-lg shadow-[var(--brand-shadow)]">
                  <Compass size={27} />
                </span>
                <h1 className="mt-6 text-3xl font-semibold tracking-[-0.035em] md:text-4xl">
                  下一站，去哪里？
                </h1>
                <p className="mt-3 max-w-md text-sm leading-6 text-[var(--muted)] md:text-base">
                  告诉我目标城市、游玩天数和开始日期，我会结合地图、路线与天气整理分日攻略。
                </p>
                <div className="mt-8 grid w-full max-w-2xl grid-cols-1 gap-2.5 sm:grid-cols-2">
                  {suggestions.map((suggestion) => (
                    <button
                      key={suggestion}
                      className="suggestion-card"
                      disabled={!currentModel?.available}
                      onClick={() => void sendMessage(suggestion)}
                    >
                      {suggestion}
                      <ArrowUp size={14} className="rotate-45 opacity-50" />
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-7">
                {messages.map((message) => (
                  <article
                    key={message.id}
                    className={`flex gap-3.5 ${message.role === "user" ? "justify-end" : "justify-start"}`}
                  >
                    {message.role === "assistant" && (
                      <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-xl bg-[var(--brand)] text-white">
                        <Compass size={16} />
                      </span>
                    )}
                    <div
                      className={
                        message.role === "user"
                          ? "max-w-[82%] rounded-2xl rounded-br-md bg-[var(--user-bubble)] px-4 py-2.5 text-[15px] leading-6"
                          : "min-w-0 max-w-[calc(100%-44px)] pt-1 text-[15px] leading-7"
                      }
                    >
                      {message.role === "assistant" ? (
                        <div>
                          {Boolean(message.debugTrace?.length) && (
                            <PlanningTracePanel traces={message.debugTrace ?? []} />
                          )}
                          {Boolean(message.planningStages?.length) && (
                            <div
                              className="mb-3 rounded-xl border border-black/[0.06] bg-black/[0.025] px-3 py-2.5"
                              aria-label="行程规划进度"
                            >
                              <div className="space-y-1.5">
                                {message.planningStages?.map((stage) => (
                                  <div
                                    key={stage.stage}
                                    className={`flex items-center gap-2 text-xs ${
                                      stage.status === "failed"
                                        ? "text-red-700"
                                        : stage.status === "partial"
                                          ? "text-amber-700"
                                        : stage.status === "skipped"
                                          ? "text-[var(--muted-light)]"
                                          : "text-[var(--muted)]"
                                    }`}
                                    title={stage.detail ?? undefined}
                                  >
                                    {stage.status === "running" ? (
                                      <LoaderCircle size={13} className="animate-spin" />
                                    ) : stage.status === "failed" ? (
                                      <X size={13} />
                                    ) : stage.status === "partial" ? (
                                      <TriangleAlert size={13} />
                                    ) : (
                                      <Check size={13} className="text-emerald-600" />
                                    )}
                                    <span>{stage.display_name}</span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                          {message.xhsLogin && (
                            <div
                              className={`mb-3 flex items-center gap-3 rounded-xl border px-3 py-3 text-sm ${
                                message.xhsLogin.status === "failed"
                                  ? "border-red-200 bg-red-50 text-red-800"
                                  : message.xhsLogin.status === "skipped"
                                    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                                  : "border-black/[0.08] bg-white"
                              }`}
                              aria-label="小红书浏览器登录"
                            >
                              {message.xhsLogin.status === "pending" ? (
                                <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-red-50 text-red-600">
                                  <LoaderCircle className="animate-spin" size={20} />
                                </div>
                              ) : message.xhsLogin.status === "skipped" ? (
                                <Check className="shrink-0" size={20} />
                              ) : (
                                <TriangleAlert className="shrink-0" size={20} />
                              )}
                              <div className="min-w-0 flex-1">
                                <p className="font-medium">
                                  {message.xhsLogin.status === "pending"
                                    ? "请在 Chrome 中登录小红书"
                                    : message.xhsLogin.status === "skipped"
                                      ? "已跳过小红书登录"
                                      : "小红书登录未完成"}
                                </p>
                                <p className="mt-1 text-xs leading-5 opacity-75">
                                  {message.xhsLogin.message}
                                </p>
                                {message.xhsLogin.status === "pending" && (
                                  <>
                                    <p className="mt-1 text-[11px] opacity-60">
                                      完成验证码或安全验证后，本次规划会自动继续。
                                    </p>
                                    {message.xhsLogin.fallback_available &&
                                      message.xhsLogin.fallback_mode === "map_weather" && (
                                        <button
                                          className="mt-2 rounded-lg border border-black/10 bg-white px-3 py-1.5 text-xs font-medium text-[var(--ink)] transition-colors hover:bg-black/[0.035]"
                                          onClick={() => void skipXhsLogin(message.id)}
                                        >
                                          跳过登录，使用地图与天气生成
                                        </button>
                                      )}
                                  </>
                                )}
                              </div>
                            </div>
                          )}
                          {Boolean(message.tools?.length) && (
                            <div className="mb-3 space-y-1.5" aria-label="工具执行状态">
                              {message.tools?.map((tool) => (
                                <div
                                  key={tool.id}
                                  className={`flex items-center gap-2 text-xs ${
                                    tool.state === "failed"
                                      ? "text-red-700"
                                      : tool.state === "partial"
                                        ? "text-amber-700"
                                        : "text-[var(--muted)]"
                                  }`}
                                  title={tool.summary}
                                >
                                  {tool.state === "running" ? (
                                    <LoaderCircle size={13} className="animate-spin" />
                                  ) : tool.state === "success" ? (
                                    <Check size={13} className="text-emerald-600" />
                                  ) : tool.state === "partial" ? (
                                    <TriangleAlert size={13} />
                                  ) : (
                                    <X size={13} />
                                  )}
                                  <span>{tool.summary ?? tool.label}</span>
                                </div>
                              ))}
                            </div>
                          )}
                          {message.content ? (
                            <>
                              <div className="markdown-body">
                                <AssistantMarkdown content={message.content} />
                              </div>
                              {message.travelPlan && (
                                <Link
                                  className="mt-5 flex w-full items-center justify-between gap-4 rounded-2xl border border-[var(--brand)]/15 bg-[var(--brand-soft)] px-4 py-3.5 text-left text-[var(--brand)] transition-colors hover:border-[var(--brand)]/25 hover:bg-[var(--brand-soft)]/70"
                                  href={`/plans/${message.travelPlan.plan_id}?version=${message.travelPlan.version}`}
                                >
                                  <span className="flex min-w-0 items-center gap-3">
                                    <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-[var(--brand)] text-white">
                                      <MapPinned size={17} />
                                    </span>
                                    <span className="min-w-0">
                                      <span className="block text-sm font-semibold text-[var(--ink)]">
                                        打开旅行计划
                                      </span>
                                      <span className="mt-0.5 block text-xs text-[var(--muted)]">
                                        地图、分日路线与出行候选 · 版本 {message.travelPlan.version}
                                      </span>
                                    </span>
                                  </span>
                                  <ArrowUp className="rotate-45" size={16} />
                                </Link>
                              )}
                            </>
                          ) : (
                            <div className="flex h-7 items-center gap-1" aria-label="正在思考">
                              <span className="thinking-dot" />
                              <span className="thinking-dot [animation-delay:140ms]" />
                              <span className="thinking-dot [animation-delay:280ms]" />
                            </div>
                          )}
                        </div>
                      ) : (
                        <p className="whitespace-pre-wrap">{message.content}</p>
                      )}
                    </div>
                    {message.role === "user" && (
                      <span className="mt-0.5 hidden size-8 shrink-0 place-items-center rounded-xl bg-black/[0.07] text-[var(--muted)] sm:grid">
                        <UserRound size={16} />
                      </span>
                    )}
                  </article>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-[var(--canvas)] via-[var(--canvas)] to-transparent px-4 pb-4 pt-12 md:px-8 md:pb-6">
          <div className="pointer-events-auto mx-auto max-w-[820px]">
            {error && (
              <div className="mb-2 flex items-center justify-between rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                <span>{error}</span>
                <button aria-label="关闭错误提示" onClick={() => setError(null)}>
                  <X size={14} />
                </button>
              </div>
            )}
            {!modelsLoading && !models.some((model) => model.available) && (
              <div className="mb-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                尚无可用模型。请先在根目录 `.env` 中配置密钥，再刷新页面。
              </div>
            )}
            <div className="composer">
              <textarea
                ref={textareaRef}
                aria-label="输入消息"
                className="max-h-[180px] min-h-7 w-full resize-none bg-transparent text-[15px] leading-7 outline-none placeholder:text-[var(--muted-light)]"
                disabled={!currentModel?.available || isLoading}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={onInputKeyDown}
                placeholder={currentModel?.available ? "说说你的旅行想法…" : "请先配置一个可用模型"}
                rows={1}
                value={input}
              />
              <div className="mt-2 flex items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2">
                  <button
                    aria-label="切换小红书原帖检索"
                    aria-pressed={planningSource === "xhs"}
                    className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                      planningSource === "xhs"
                        ? "border-red-200 bg-red-50 text-red-700"
                        : "border-black/[0.08] bg-white/70 text-[var(--muted)] hover:bg-black/[0.035]"
                    }`}
                    disabled={isLoading || !currentModel?.available}
                    onClick={() =>
                      setPlanningSource((current) => (current === "xhs" ? "standard" : "xhs"))
                    }
                    title="直接搜索小红书并返回最多两篇原帖正文，不经过 AI 改写"
                    type="button"
                  >
                    <BookOpenText size={13} />
                    小红书原帖
                  </button>
                  <span className="hidden truncate text-[11px] text-[var(--muted-light)] sm:inline">
                    {currentModel
                      ? `${currentModel.display_name} · AI 可能会出错，请核实重要信息`
                      : "未选择模型"}
                  </span>
                </div>
                {isLoading ? (
                  <button className="send-button" aria-label="停止生成" onClick={stopGeneration}>
                    <CircleStop size={17} />
                  </button>
                ) : (
                  <button
                    className="send-button"
                    aria-label="发送消息"
                    disabled={!canSend}
                    onClick={() => void sendMessage()}
                  >
                    <ArrowUp size={18} strokeWidth={2.5} />
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  );
}
