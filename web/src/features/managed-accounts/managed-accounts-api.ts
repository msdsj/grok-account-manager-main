export type ManagedProvider = "grok" | "chatgpt" | "kiro";

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
    error?: string | null;
  };
};

export type AccountTag = { tag: string; count: number };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = (await response.json().catch(() => ({}))) as T & { error?: string };
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
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

export async function exportManagedAccounts(exportKeys: string[]): Promise<Blob> {
  const response = await fetch("/api/accounts/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ exportKeys }),
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { error?: string };
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return response.blob();
}
