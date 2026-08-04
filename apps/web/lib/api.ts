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

export type AuthUser = {
  id: string;
  phone: string;
  display_name: string | null;
};

export type SmsCodeChallenge = {
  challenge_id: string;
  expires_in: number;
  resend_after: number;
  debug_code?: string;
};

export type LoginResult = {
  user: AuthUser;
  is_new_user: boolean;
  access_expires_in: number;
};

export type AuthState = {
  user: AuthUser;
  access_expires_in: number;
};

export type ApiChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

export type PlanningSource = "standard" | "xhs";

export type ConversationSummary = {
  id: string;
  title: string;
  model_id: string;
  planning_source: PlanningSource;
  created_at: string;
  updated_at: string;
};

export type PersistedMessage = ApiChatMessage & {
  sequence: number;
  status: "streaming" | "completed" | "failed" | "interrupted";
  debug_trace: PlanningTraceUpdate[];
  travel_plan?: TravelPlanReference | null;
  created_at: string;
};

export type ConversationDetail = ConversationSummary & {
  messages: PersistedMessage[];
  tool_calls?: PersistedToolCall[];
};

export type PersistedToolCall = {
  id: string;
  assistant_message_id: string;
  process_status: "not_started" | "success" | "failed" | "timeout" | null;
  process_return_code: number | null;
  provider_status: "unknown" | "success" | "failed" | null;
  parse_status: "not_attempted" | "success" | "invalid" | "empty" | null;
  business_status: "unknown" | "usable" | "empty" | "invalid" | null;
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
  process_status: "not_started" | "success" | "failed" | "timeout" | null;
  process_return_code: number | null;
  provider_status: "unknown" | "success" | "failed" | null;
  parse_status: "not_attempted" | "success" | "invalid" | "empty" | null;
  business_status: "unknown" | "usable" | "empty" | "invalid" | null;
};

export type PlanningStageUpdate = {
  stage: string;
  display_name: string;
  status: "running" | "success" | "partial" | "failed" | "skipped";
  detail: string | null;
};

export type PlanningTraceUpdate = {
  type: "planning_trace";
  sequence: number;
  step:
    | "request_received"
    | "route_selected"
    | "requirements_extracted"
    | "requirements_validated"
    | "login_checked"
    | "login_completed"
    | "search_query_built"
    | "search_results"
    | "post_detail"
    | "evidence_selected"
    | "itinerary_skeleton_ready"
    | "itinerary_generated"
    | "validation_completed"
    | "response_completed";
  title: string;
  status: "running" | "success" | "partial" | "failed" | "skipped";
  detail: string | null;
  duration_ms: number | null;
  data: Record<string, unknown>;
  occurred_at: string;
};

export type XhsLoginRequiredUpdate = {
  login_id: string;
  expires_at: string;
  message: string;
  fallback_available?: boolean;
  fallback_mode?: "map_weather" | null;
};

export type TravelPlanReference = {
  plan_id: string;
  version_id: string;
  version: number;
};

export type TripPlanCoordinate = {
  longitude: number;
  latitude: number;
  coordinate_system: "GCJ02";
  source: "amap" | "amap_conversion";
};

export type TripPlanPlace = {
  plan_item_id: string;
  provider: "amap";
  provider_place_id: string;
  reference_id: string;
  name: string;
  address: string;
  poi_type: string;
  location: TripPlanCoordinate;
  adcode: string | null;
  city: string | null;
  source_query: string;
  source_rank: number;
  candidate_score: number;
  estimated_visit_minutes: number;
  matched_preferences: string[];
  selection_reasons: string[];
};

export type TripPlanRouteLeg = {
  origin_plan_item_id: string;
  destination_plan_item_id: string;
  mode: "walking" | "transit" | "driving" | "estimated" | "unverified";
  distance_meters: number | null;
  duration_seconds: number | null;
  transfer_count: number | null;
  route_summary: string | null;
  is_fallback: boolean;
};

export type TripPlanWeather = {
  provider: "amap";
  queried_at: string | null;
  coverage: "available" | "unavailable";
  day_weather: string | null;
  night_weather: string | null;
  day_temperature: string | null;
  night_temperature: string | null;
  day_wind_direction: string | null;
  night_wind_direction: string | null;
  day_wind_power: string | null;
  night_wind_power: string | null;
  advice: string[];
  unavailable_reason: string | null;
};

export type TripPlanDay = {
  day_id: string;
  day_index: number;
  date: string;
  places: TripPlanPlace[];
  route_legs: TripPlanRouteLeg[];
  weather: TripPlanWeather;
  estimated_visit_minutes: number;
  estimated_transport_minutes: number;
  warnings: string[];
};

export type TransportOption = {
  kind: "transport";
  option_id: string;
  provider: "flyai";
  mode: "flight" | "train";
  direction: "outbound" | "return";
  journey_type: string | null;
  transport_names: string[];
  transport_numbers: string[];
  departure_station: string;
  departure_at: string;
  arrival_station: string;
  arrival_at: string;
  duration_minutes: number | null;
  seat_classes: string[];
  price_amount: string | number | null;
  currency: "CNY" | null;
  detail_url: string | null;
  display_text: string;
};

export type HotelOption = {
  kind: "hotel";
  option_id: string;
  provider: "flyai";
  provider_hotel_id: string | null;
  name: string;
  star: string | null;
  price_amount: string | number | null;
  currency: "CNY" | null;
  nearby_poi: string | null;
  address: string | null;
  detail_url: string | null;
  display_text: string;
};

type CapabilityStatus = "skipped" | "usable" | "empty" | "failed";

export type TravelPlanSnapshotV1 = {
  schema_version: "trip_plan.v1";
  request: {
    core: {
      destination_city: string | null;
      duration_days: number | null;
      start_date: string | null;
      interests: string[];
      food_preferences: string[];
    };
  };
  capabilities: {
    derivations: Array<{
      field: string;
      value: string;
      source: string;
      explanation: string;
    }>;
  };
  days: TripPlanDay[];
  transport: {
    enabled: boolean;
    status: CapabilityStatus;
    queried_at: string | null;
    modes: Array<"flight" | "train">;
    journey_scope: string;
    origin: string | null;
    destination: string | null;
    outbound_date: string | null;
    return_date: string | null;
    options: TransportOption[];
    warnings: string[];
  };
  hotel: {
    enabled: boolean;
    status: CapabilityStatus;
    queried_at: string | null;
    destination: string | null;
    check_in_date: string | null;
    check_out_date: string | null;
    nearby_poi: string | null;
    options: HotelOption[];
    warnings: string[];
  };
  overall_status: "usable" | "partial" | "failed";
  warnings: string[];
  source_metadata: {
    planning_run_id: string;
    generated_at: string;
    map_queried_at: string | null;
    weather_queried_at: string | null;
    transport_queried_at: string | null;
    hotel_queried_at: string | null;
  };
};

export type TripPreference =
  | "历史文化"
  | "博物馆展览"
  | "自然风光"
  | "城市地标"
  | "特色街区"
  | "摄影打卡"
  | "亲子游"
  | "休闲慢游"
  | "夜景体验";

export type StructuredTripRequest = {
  destination_city: string;
  start_date: string;
  duration_days: number;
  interests: TripPreference[];
};

export type RestaurantRecommendation = {
  provider_place_id: string;
  name: string;
  address: string;
  poi_type: string;
  rating: number | null;
  business_area: string | null;
  city: string | null;
  adcode: string | null;
  location: TripPlanCoordinate;
  source_queries: string[];
  best_search_rank: number;
  selection_reasons: string[];
  recommendation_reason: string;
};

export type TravelPlanSnapshotV2 = {
  schema_version: "trip_plan.v2";
  request: StructuredTripRequest;
  days: TripPlanDay[];
  restaurant_recommendations: RestaurantRecommendation[];
  overall_status: "usable" | "partial" | "failed";
  warnings: string[];
  source_metadata: {
    planning_run_id: string;
    generated_at: string;
    map_queried_at: string | null;
    weather_queried_at: string | null;
    restaurant_queried_at: string | null;
  };
};

export type TravelPlanSnapshot = TravelPlanSnapshotV1 | TravelPlanSnapshotV2;

export type TravelPlanSummary = {
  plan_id: string;
  title: string;
  status: "draft" | "active" | "archived";
  current_version: number;
  destination_city: string;
  start_date: string;
  duration_days: number;
  created_at: string;
  updated_at: string;
};

export type TripNarrative = {
  title: string;
  summary: string;
  days: Array<{
    day_index: number;
    date: string;
    theme: string;
    places: Array<{ reference_id: string; recommendation_reason: string }>;
    weather_advice: string[];
    tips: string[];
  }>;
  practical_tips: string[];
  warnings: string[];
};

export type TravelPlanDetail = TravelPlanReference & {
  title: string;
  status: "draft" | "active" | "archived";
  current_version: number;
  change_summary: string | null;
  created_at: string;
  snapshot: TravelPlanSnapshot;
  narrative: TripNarrative | null;
  rendered_markdown: string | null;
};

export type DirectTravelSearchResponse = {
  kind: "hotel" | "flight" | "train";
  tool_call_id: string;
  tool_name: "search_hotel" | "search_flight" | "search_train";
  arguments: Record<string, unknown>;
  success: boolean;
  summary: string;
  options: Array<HotelOption | TransportOption>;
  error_code: string | null;
  provider_error_code: string | null;
  provider_item_count: number | null;
  rejected_item_count: number;
  queried_at: string;
};

export type HotelSearchRequest = {
  destination: string;
  check_in_date: string;
  check_out_date: string;
  keywords?: string;
  nearby_poi?: string;
  hotel_stars?: number[];
  max_price?: number;
};

export type TransportSearchRequest = {
  origin: string;
  destination: string;
  departure_date: string;
  return_date?: string;
  max_price?: number;
};

type StreamCallbacks = {
  onToken: (delta: string) => void;
  onConversation?: (conversation: {
    id: string;
    title: string;
    planning_source: PlanningSource;
  }) => void;
  onToolCall?: (update: ToolCallUpdate) => void;
  onToolResult?: (update: ToolResultUpdate) => void;
  onPlanningStage?: (update: PlanningStageUpdate) => void;
  onPlanningTrace?: (update: PlanningTraceUpdate) => void;
  onXhsLoginRequired?: (update: XhsLoginRequiredUpdate) => void;
  onTravelPlanReady?: (update: TravelPlanReference) => void;
  onDone?: () => void;
};

const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export const AUTH_EXPIRED_EVENT = "tour-assistant:auth-expired";

let refreshPromise: Promise<AuthState | null> | null = null;

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}

function requestInit(init: RequestInit = {}): RequestInit {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = readCookie("ta_csrf");
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  return {
    ...init,
    headers,
    credentials: "include",
  };
}

function notifyAuthExpired(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
}

async function responseError(response: Response): Promise<Error> {
  try {
    const body = (await response.json()) as { detail?: string };
    return new Error(body.detail || `请求失败（${response.status}）`);
  } catch {
    return new Error(`请求失败（${response.status}）`);
  }
}

async function authenticatedFetch(url: string, init: RequestInit = {}): Promise<Response> {
  let response = await fetch(url, requestInit(init));
  if (response.status !== 401) return response;

  const refreshed = await refreshAuthentication();
  if (!refreshed) {
    notifyAuthExpired();
    return response;
  }
  response = await fetch(url, requestInit(init));
  if (response.status === 401) notifyAuthExpired();
  return response;
}

export async function requestSmsCode(phone: string): Promise<SmsCodeChallenge> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/auth/sms-codes`,
    requestInit({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone }),
    }),
  );
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as SmsCodeChallenge;
}

export async function loginWithPhone(
  phone: string,
  challengeId: string,
  code: string,
): Promise<LoginResult> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/auth/phone-login`,
    requestInit({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone, challenge_id: challengeId, code }),
    }),
  );
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as LoginResult;
}

export async function fetchCurrentUser(signal?: AbortSignal): Promise<AuthUser | null> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/auth/me`,
    requestInit({ signal, cache: "no-store" }),
  );
  if (response.status === 401) return null;
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as AuthUser;
}

export function refreshAuthentication(): Promise<AuthState | null> {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/auth/refresh`,
      requestInit({ method: "POST", cache: "no-store" }),
    );
    if (response.status === 401 || response.status === 403) return null;
    if (!response.ok) throw await responseError(response);
    return (await response.json()) as AuthState;
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

export async function logoutAuthentication(): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/auth/logout`,
    requestInit({ method: "POST", cache: "no-store" }),
  );
  if (!response.ok) throw await responseError(response);
}

export async function fetchModels(signal?: AbortSignal): Promise<ModelList> {
  const response = await fetch(`${API_BASE_URL}/api/v1/models`, {
    signal,
    cache: "no-store",
    credentials: "include",
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as ModelList;
}

export async function fetchConversations(signal?: AbortSignal): Promise<ConversationSummary[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/api/v1/conversations`, {
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
  const response = await authenticatedFetch(
    `${API_BASE_URL}/api/v1/conversations/${conversationId}`,
    {
      signal,
      cache: "no-store",
    },
  );
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as ConversationDetail;
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/api/v1/conversations/${conversationId}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw await responseError(response);
  }
}

export async function fetchTravelPlan(
  planId: string,
  version?: number,
  signal?: AbortSignal,
): Promise<TravelPlanDetail> {
  const search = version ? `?version=${encodeURIComponent(version)}` : "";
  const response = await authenticatedFetch(
    `${API_BASE_URL}/api/v1/travel-plans/${encodeURIComponent(planId)}${search}`,
    { signal, cache: "no-store" },
  );
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as TravelPlanDetail;
}

export async function fetchTravelPlans(signal?: AbortSignal): Promise<TravelPlanSummary[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/api/v1/travel-plans`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as TravelPlanSummary[];
}

export async function deleteTravelPlan(planId: string): Promise<void> {
  const response = await authenticatedFetch(
    `${API_BASE_URL}/api/v1/travel-plans/${encodeURIComponent(planId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) throw await responseError(response);
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
  planningSource: PlanningSource = "standard",
  activePlanId: string | null = null,
  activePlanVersion: number | null = null,
): Promise<void> {
  const response = await authenticatedFetch(`${API_BASE_URL}/api/v1/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model_id: modelId,
      message,
      conversation_id: conversationId,
      planning_source: planningSource,
      active_plan_id: activePlanId,
      active_plan_version: activePlanVersion,
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
      const data = parsed.data as {
        id?: string;
        title?: string;
        planning_source?: PlanningSource;
      };
      if (data.id && data.title && data.planning_source) {
        callbacks.onConversation?.({
          id: data.id,
          title: data.title,
          planning_source: data.planning_source,
        });
      }
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
          process_status: data.process_status ?? null,
          process_return_code: data.process_return_code ?? null,
          provider_status: data.provider_status ?? null,
          parse_status: data.parse_status ?? null,
          business_status: data.business_status ?? null,
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
    } else if (parsed.event === "planning_trace") {
      const data = parsed.data as Partial<PlanningTraceUpdate>;
      if (
        data.type === "planning_trace" &&
        typeof data.sequence === "number" &&
        data.step &&
        data.title &&
        data.status &&
        data.data &&
        data.occurred_at
      ) {
        callbacks.onPlanningTrace?.(data as PlanningTraceUpdate);
      }
    } else if (parsed.event === "xhs_login_required") {
      const data = parsed.data as Partial<XhsLoginRequiredUpdate>;
      if (
        data.login_id &&
        data.expires_at &&
        data.message
      ) {
        callbacks.onXhsLoginRequired?.(data as XhsLoginRequiredUpdate);
      }
    } else if (parsed.event === "travel_plan_ready") {
      const data = parsed.data as Partial<TravelPlanReference>;
      if (data.plan_id && data.version_id && typeof data.version === "number") {
        callbacks.onTravelPlanReady?.(data as TravelPlanReference);
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

export async function streamTravelPlan(
  modelId: string,
  tripRequest: StructuredTripRequest,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
  planId: string | null = null,
): Promise<void> {
  const response = await authenticatedFetch(`${API_BASE_URL}/api/v1/travel-plans/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId, plan_id: planId, request: tripRequest }),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("浏览器无法读取规划流。 ");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (frame: string) => {
    const parsed = parseEventFrame(frame.replaceAll("\r\n", "\n"));
    if (!parsed) return;
    if (parsed.event === "message_delta") {
      const data = parsed.data as { delta?: string };
      if (data.delta) callbacks.onToken(data.delta);
    } else if (parsed.event === "planning_stage") {
      const data = parsed.data as PlanningStageUpdate;
      callbacks.onPlanningStage?.(data);
    } else if (parsed.event === "planning_trace") {
      callbacks.onPlanningTrace?.(parsed.data as PlanningTraceUpdate);
    } else if (parsed.event === "travel_plan_ready") {
      callbacks.onTravelPlanReady?.(parsed.data as TravelPlanReference);
    } else if (parsed.event === "done") {
      callbacks.onDone?.();
    } else if (parsed.event === "error") {
      const data = parsed.data as { message?: string };
      throw new Error(data.message || "旅行规划生成中断。");
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

async function directTravelSearch(
  endpoint: "hotels" | "flights" | "trains",
  payload: HotelSearchRequest | TransportSearchRequest,
  signal?: AbortSignal,
): Promise<DirectTravelSearchResponse> {
  const response = await authenticatedFetch(`${API_BASE_URL}/api/v1/search/${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  return (await response.json()) as DirectTravelSearchResponse;
}

export function searchHotels(
  request: HotelSearchRequest,
  signal?: AbortSignal,
): Promise<DirectTravelSearchResponse> {
  return directTravelSearch("hotels", request, signal);
}

export function searchFlights(
  request: TransportSearchRequest,
  signal?: AbortSignal,
): Promise<DirectTravelSearchResponse> {
  return directTravelSearch("flights", request, signal);
}

export function searchTrains(
  request: TransportSearchRequest,
  signal?: AbortSignal,
): Promise<DirectTravelSearchResponse> {
  return directTravelSearch("trains", request, signal);
}
