export type GatewayScope = "chat" | "image" | "video";

export type GatewayKeyDTO = {
  id: string;
  name: string;
  keyPrefix: string;
  scopes: string[];
  models: string[];
  enabled: boolean;
  createdAt: number;
  lastUsedAt: number | null;
};

export type CreatedGatewayKeyDTO = {
  id: string;
  name: string;
  key: string;
  keyPrefix: string;
  scopes: string[];
  models: string[];
};

export type GatewayRequestDTO = {
  id: string;
  clientKeyId: string;
  provider: string;
  model: string;
  capability: string;
  accountId: string;
  status: string;
  httpStatus: number;
  latencyMs: number;
  stream: boolean;
  promptTokens: number;
  completionTokens: number;
  error: string;
  createdAt: number;
};

export type GatewayModelDTO = {
  publicModel: string;
  provider: string;
  upstreamModel: string;
  capability: string;
  enabled: boolean;
  weight: number;
  config: Record<string, unknown>;
};

export type GatewayProviderStatusDTO = {
  provider: string;
  accounts: number;
  models: number;
};

export type GatewayStatusDTO = {
  providers: GatewayProviderStatusDTO[];
};

type GatewayRequestOptions = {
  method?: "GET" | "POST" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
};

function readGatewayError(payload: unknown): string | undefined {
  if (typeof payload !== "object" || payload === null) return undefined;
  const record = payload as Record<string, unknown>;
  const error = record.error;
  if (typeof error === "string" && error) return error;
  if (typeof error === "object" && error !== null) {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === "string" && message) return message;
  }
  if (typeof record.detail === "string" && record.detail) return record.detail;
  return undefined;
}

async function gatewayRequest<T>(path: string, options: GatewayRequestOptions = {}): Promise<T> {
  const headers = new Headers();
  let body: string | undefined;
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }
  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body,
    signal: options.signal,
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(readGatewayError(payload) ?? `HTTP ${response.status}`);
  }
  return payload as T;
}

export async function listGatewayKeys(): Promise<GatewayKeyDTO[]> {
  const result = await gatewayRequest<{ items?: GatewayKeyDTO[] }>("/api/gateway/keys");
  return result.items ?? [];
}

export async function createGatewayKey(input: { name: string; scopes: string[]; models: string[] }): Promise<CreatedGatewayKeyDTO> {
  const result = await gatewayRequest<{ key: CreatedGatewayKeyDTO }>("/api/gateway/keys", {
    method: "POST",
    body: input,
  });
  return result.key;
}

export async function deleteGatewayKey(id: string): Promise<number> {
  const result = await gatewayRequest<{ deleted?: number }>(`/api/gateway/keys/${encodeURIComponent(id)}`, { method: "DELETE" });
  return result.deleted ?? 0;
}

export async function listGatewayRequests(input: {
  provider?: string;
  status?: string;
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}): Promise<{ items: GatewayRequestDTO[]; total: number }> {
  const query = new URLSearchParams();
  if (input.provider) query.set("provider", input.provider);
  if (input.status) query.set("status", input.status);
  query.set("limit", String(input.limit ?? 20));
  query.set("offset", String(input.offset ?? 0));
  const result = await gatewayRequest<{ items?: GatewayRequestDTO[]; total?: number }>(`/api/gateway/requests?${query.toString()}`, {
    signal: input.signal,
  });
  return { items: result.items ?? [], total: result.total ?? 0 };
}

export async function listGatewayModels(signal?: AbortSignal): Promise<GatewayModelDTO[]> {
  const result = await gatewayRequest<{ items?: GatewayModelDTO[] }>("/api/gateway/models", { signal });
  return result.items ?? [];
}

export async function getGatewayStatus(signal?: AbortSignal): Promise<GatewayStatusDTO> {
  return gatewayRequest<GatewayStatusDTO>("/api/gateway/status", { signal });
}

export function formatGatewayTimestamp(value: number | string | null | undefined, locale: string): string {
  if (value === null || value === undefined || value === "") return "-";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}
