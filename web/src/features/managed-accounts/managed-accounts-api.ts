export type ManagedProvider = "grok" | "chatgpt" | "kiro";

export type AccountTestMode = "chat" | "image" | "video";

export type ManagedAccount = {
  id: string;
  exportKey: string;
  provider: ManagedProvider | string;
  email: string;
  displayName?: string;
  authMode?: string;
  planType?: string;
  userId?: string;
  createdAt?: number;
  createdAtLabel?: string;
  hasRefreshToken?: boolean;
  hasAccessToken?: boolean;
  oauthStatus?: string;
  oauthError?: string;
  tags?: string[];
  quota?: {
    frequentUsage?: number | null;
    frequentLimit?: number | null;
    occasionalUsage?: number | null;
    occasionalLimit?: number | null;
    weeklyUsed?: number | null;
    weeklyTotal?: number | null;
    weeklyLimitPercent?: number | null;
  };
  availability?: {
    category?: string;
    chatAvailable?: boolean;
    imageAvailable?: boolean;
    videoAvailable?: boolean;
    chatModel?: string | null;
    imageModel?: string | null;
    videoModel?: string | null;
    error?: string | null;
    latencyMs?: number | null;
    testedAt?: number | null;
  };
};

export type AccountTag = { tag: string; count: number };

export type AccountTestImage = {
  url?: string;
  b64_json?: string;
  revised_prompt?: string;
};

export type AccountTestResult = {
  provider?: string;
  model?: string;
  message?: { role: string; content: string };
  data?: AccountTestImage[];
  video?: Record<string, unknown>;
  account?: ManagedAccount;
  accounts?: ManagedAccount[];
};

type ApiErrorPayload = { error?: unknown; detail?: unknown };

function errorMessage(payload: ApiErrorPayload, status: number): string {
  const detail = typeof payload.detail === "string" ? payload.detail : "";
  const error = typeof payload.error === "string" ? payload.error : "";
  if (error) return error;
  if (detail) return detail;
  return `HTTP ${status}`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = (await response.json().catch(() => ({}))) as T & ApiErrorPayload;
  if (!response.ok) {
    throw new Error(errorMessage(payload, response.status));
  }
  return payload;
}

export async function listManagedAccounts(input: {
  provider?: string;
  search?: string;
  tag?: string;
}): Promise<{ accounts: ManagedAccount[]; total: number }> {
  const query = new URLSearchParams();
  if (input.provider) query.set("provider", input.provider);
  if (input.search) query.set("search", input.search);
  if (input.tag) query.set("tag", input.tag);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return api<{ accounts: ManagedAccount[]; total: number }>(`/api/accounts${suffix}`);
}

export async function listAccountTags(): Promise<AccountTag[]> {
  const result = await api<{ items: AccountTag[] }>("/api/accounts/tags");
  return result.items ?? [];
}

export function updateAccountTags(
  exportKeys: string[],
  tags: string[],
  mode: "replace" | "add" | "remove" = "replace",
): Promise<{ updated: number; items: AccountTag[] }> {
  return api<{ updated: number; items: AccountTag[] }>("/api/accounts/tags", {
    method: "PUT",
    body: JSON.stringify({ exportKeys, tags, mode }),
  });
}

export function deleteManagedAccounts(exportKeys: string[]): Promise<{ deleted: number }> {
  return api<{ deleted: number }>("/api/accounts/delete", {
    method: "POST",
    body: JSON.stringify({ exportKeys }),
  });
}

export function refreshManagedAccountQuota(accountId: string): Promise<{ account: ManagedAccount }> {
  return api<{ account: ManagedAccount }>("/api/accounts/refresh-quota", {
    method: "POST",
    body: JSON.stringify({ accountId }),
  });
}

export function testManagedAccountChat(input: {
  exportKey: string;
  model: string;
  messages: Array<{ role: string; content: string }>;
}): Promise<AccountTestResult> {
  return api<AccountTestResult>("/api/accounts/chat-test", {
    method: "POST",
    body: JSON.stringify({ ...input, timeout: 180 }),
  });
}

export function testManagedAccountImage(input: {
  exportKey: string;
  model: string;
  prompt: string;
  n?: number;
  size?: string;
}): Promise<AccountTestResult> {
  return api<AccountTestResult>("/api/accounts/image-test", {
    method: "POST",
    body: JSON.stringify({ ...input, n: input.n ?? 1, size: input.size ?? "1024x1024", timeout: 180 }),
  });
}

export function testManagedAccountVideo(input: {
  exportKey: string;
  model: string;
  prompt: string;
  duration?: number;
  resolution?: string;
  aspectRatio?: string;
}): Promise<AccountTestResult> {
  return api<AccountTestResult>("/api/accounts/video-test", {
    method: "POST",
    body: JSON.stringify({
      ...input,
      duration: input.duration ?? 6,
      resolution: input.resolution ?? "720p",
      aspectRatio: input.aspectRatio ?? "16:9",
      timeout: 180,
    }),
  });
}

export function getManagedAccountVideoStatus(input: {
  exportKey: string;
  videoId: string;
}): Promise<AccountTestResult> {
  return api<AccountTestResult>("/api/accounts/video-status", {
    method: "POST",
    body: JSON.stringify({ ...input, timeout: 180 }),
  });
}

export async function exportManagedAccounts(exportKeys: string[]): Promise<Blob> {
  const response = await fetch("/api/accounts/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ exportKeys }),
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ApiErrorPayload;
    throw new Error(errorMessage(payload, response.status));
  }
  return response.blob();
}
