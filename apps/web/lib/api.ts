export type ModelInfo = {
  id: string;
  display_name: string;
  description: string;
  provider: string;
  available: boolean;
  unavailable_reason: string | null;
};

export type ModelList = {
  default_model: string | null;
  models: ModelInfo[];
};

export type ApiChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

export type ConversationSummary = {
  id: string;
  title: string;
  model_id: string;
  created_at: string;
  updated_at: string;
};

export type PersistedMessage = ApiChatMessage & {
  sequence: number;
  status: "streaming" | "completed" | "failed" | "interrupted";
  created_at: string;
};

export type ConversationDetail = ConversationSummary & {
  messages: PersistedMessage[];
  tool_calls?: PersistedToolCall[];
};

export type PersistedToolCall = {
  id: string;
  assistant_message_id: string;
  tool_call_id: string;
  tool_name: string;
  provider: string;
  status: "pending" | "success" | "failed";
  result_summary: string;
  error_code: string | null;
  provider_error_code: string | null;
  duration_ms: number;
  data_status: "usable" | "partial" | "empty" | "invalid" | null;
  provider_item_count: number | null;
  normalized_item_count: number | null;
  rejected_item_count: number | null;
  schema_version: string | null;
  created_at: string;
};

export type ToolCallUpdate = {
  tool_call_id: string;
  tool_name: string;
  display_name: string;
};

export type ToolResultUpdate = {
  tool_call_id: string;
  tool_name: string;
  success: boolean;
  summary: string;
  duration_ms: number;
  error_code: string | null;
  provider_error_code: string | null;
  data_status: "usable" | "partial" | "empty" | "invalid" | null;
  provider_item_count: number | null;
  normalized_item_count: number | null;
  rejected_item_count: number | null;
  schema_version: string | null;
};

export type PlanningStageUpdate = {
  stage: string;
  display_name: string;
  status: "running" | "success" | "partial" | "failed" | "skipped";
  detail: string | null;
};

type StreamCallbacks = {
  onToken: (delta: string) => void;
  onConversation?: (conversation: { id: string; title: string }) => void;
  onToolCall?: (update: ToolCallUpdate) => void;
  onToolResult?: (update: ToolResultUpdate) => void;
  onPlanningStage?: (update: PlanningStageUpdate) => void;
  onDone?: () => void;
};

const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

async function responseError(response: Response): Promise<Error> {
  try {
    const body = (await response.json()) as { detail?: string };
    return new Error(body.detail || `请求失败（${response.status}）`);
  } catch {
    return new Error(`请求失败（${response.status}）`);
  }
}

export async function fetchModels(signal?: AbortSignal): Promise<ModelList> {
  const response = await fetch(`${API_BASE_URL}/api/v1/models`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as ModelList;
}

export async function fetchConversations(signal?: AbortSignal): Promise<ConversationSummary[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/conversations`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as ConversationSummary[];
}

export async function fetchConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationDetail> {
  const response = await fetch(`${API_BASE_URL}/api/v1/conversations/${conversationId}`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as ConversationDetail;
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/conversations/${conversationId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw await responseError(response);
  }
}

function parseEventFrame(frame: string): { event: string; data: unknown } | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  if (dataLines.length === 0) {
    return null;
  }
  return { event, data: JSON.parse(dataLines.join("\n")) as unknown };
}

export async function streamChat(
  modelId: string,
  message: string,
  conversationId: string | null,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model_id: modelId,
      message,
      conversation_id: conversationId,
    }),
    signal,
  });

  if (!response.ok) {
    throw await responseError(response);
  }
  if (!response.body) {
    throw new Error("浏览器无法读取流式响应。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const consume = (frame: string) => {
    const parsed = parseEventFrame(frame.replaceAll("\r\n", "\n"));
    if (!parsed) return;
    if (parsed.event === "conversation") {
      const data = parsed.data as { id?: string; title?: string };
      if (data.id && data.title) callbacks.onConversation?.({ id: data.id, title: data.title });
    } else if (parsed.event === "message_delta" || parsed.event === "token") {
      const data = parsed.data as { delta?: string };
      if (data.delta) callbacks.onToken(data.delta);
    } else if (parsed.event === "tool_call") {
      const data = parsed.data as Partial<ToolCallUpdate>;
      if (data.tool_call_id && data.tool_name && data.display_name) {
        callbacks.onToolCall?.(data as ToolCallUpdate);
      }
    } else if (parsed.event === "tool_result") {
      const data = parsed.data as Partial<ToolResultUpdate>;
      if (
        data.tool_call_id &&
        data.tool_name &&
        typeof data.success === "boolean" &&
        data.summary &&
        typeof data.duration_ms === "number"
      ) {
        callbacks.onToolResult?.({
          tool_call_id: data.tool_call_id,
          tool_name: data.tool_name,
          success: data.success,
          summary: data.summary,
          duration_ms: data.duration_ms,
          error_code: data.error_code ?? null,
          provider_error_code: data.provider_error_code ?? null,
          data_status: data.data_status ?? null,
          provider_item_count: data.provider_item_count ?? null,
          normalized_item_count: data.normalized_item_count ?? null,
          rejected_item_count: data.rejected_item_count ?? null,
          schema_version: data.schema_version ?? null,
        });
      }
    } else if (parsed.event === "planning_stage") {
      const data = parsed.data as Partial<PlanningStageUpdate>;
      if (
        data.stage &&
        data.display_name &&
        (data.status === "running" ||
          data.status === "success" ||
          data.status === "partial" ||
          data.status === "failed" ||
          data.status === "skipped")
      ) {
        callbacks.onPlanningStage?.({
          stage: data.stage,
          display_name: data.display_name,
          status: data.status,
          detail: data.detail ?? null,
        });
      }
    } else if (parsed.event === "done") {
      callbacks.onDone?.();
    } else if (parsed.event === "error") {
      const data = parsed.data as { message?: string };
      throw new Error(data.message || "模型响应中断。");
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      consume(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }

    if (done) break;
  }
  if (buffer.trim()) consume(buffer);
}
