export type EmailSource = "duckmail" | "outlook" | "google";

export type JobStatus = "running" | "stopping" | "completed" | "completed_with_errors" | "stopped";

export interface WorkerState {
  worker: number;
  status: string;
  round: number | null;
  email: string;
  stage: string;
  message: string;
  fingerprint: string;
  proxy: string;
  updatedAt: number;
}

export interface JobEvent {
  level: "info" | "warning" | "error";
  message: string;
  at: number;
}

export interface RegistrationJob {
  id: string;
  status: JobStatus;
  total: number;
  concurrency: number;
  oauthExchange: boolean;
  windowsMinimized: boolean;
  emailSource: string;
  proxyPoolEnabled: boolean;
  proxyPoolSource: string;
  proxyPoolTotal: number;
  proxyPoolUsed: number;
  proxyPoolRemaining: number;
  issued: number;
  completed: number;
  failed: number;
  registered: number;
  workers: WorkerState[];
  events: JobEvent[];
  startedAt: number;
  finishedAt: number | null;
}

export interface StartRegistrationInput {
  total: number;
  concurrency: number;
  oauthExchange: boolean;
  minimizeBrowsers: boolean;
  emailSource: EmailSource;
  outlookData?: string;
  outlookAccountsFile?: string;
  googleData?: string;
  googleAccountsFile?: string;
  proxyPoolEnabled?: boolean;
  proxyFile?: string;
}

export interface OutlookMailboxPool {
  data: string;
  count: number;
  invalid: number;
  accounts: Array<{
    email: string;
    mode: "auto" | "imap" | "graph";
  }>;
}

export interface RegistrationProxyPool {
  count: number;
  items: string[];
}

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

export async function getCurrentJob(): Promise<RegistrationJob | null> {
  const result = await api<{ job: RegistrationJob | null }>("/api/jobs/current");
  return result.job;
}

export async function startRegistration(input: StartRegistrationInput): Promise<RegistrationJob> {
  const result = await api<{ job: RegistrationJob }>("/api/register", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return result.job;
}

export async function stopRegistration(): Promise<RegistrationJob | null> {
  const result = await api<{ job: RegistrationJob | null }>("/api/register/stop", { method: "POST" });
  return result.job;
}

export async function retryRegistration(): Promise<RegistrationJob> {
  const result = await api<{ job: RegistrationJob }>("/api/register/retry", { method: "POST" });
  return result.job;
}

export async function syncAccountsToPool(): Promise<{ requested: number }> {
  return api<{ requested: number }>("/api/relay/sync-accounts", {
    method: "POST",
    body: JSON.stringify({ exportKeys: [] }),
  });
}

export function getOutlookMailboxPool(): Promise<OutlookMailboxPool> {
  return api<OutlookMailboxPool>("/api/mailboxes/outlook");
}

export function getRegistrationProxyPool(): Promise<RegistrationProxyPool> {
  return api<RegistrationProxyPool>("/api/register/proxies");
}

export function saveRegistrationProxyPool(data: string, replace = false): Promise<RegistrationProxyPool & { added: number; skipped: number }> {
  return api<RegistrationProxyPool & { added: number; skipped: number }>("/api/register/proxies", {
    method: "PUT",
    body: JSON.stringify({ data, replace }),
  });
}

export function clearRegistrationProxyPool(): Promise<RegistrationProxyPool & { removed: number }> {
  return api<RegistrationProxyPool & { removed: number }>("/api/register/proxies", { method: "DELETE" });
}

export function saveOutlookMailboxPool(data: string): Promise<Omit<OutlookMailboxPool, "data" | "invalid">> {
  return api<Omit<OutlookMailboxPool, "data" | "invalid">>("/api/mailboxes/outlook", {
    method: "PUT",
    body: JSON.stringify({ data }),
  });
}

function splitAccountFields(entry: string): string[] {
  const value = entry.trim();
  if (!value) return [];
  if (/-{4,}/.test(value)) return value.split(/-{4,}/).map((part) => part.trim());
  if (value.includes("|")) return value.split("|").map((part) => part.trim());
  return [value];
}

export function countOutlookAccounts(data: string): number {
  const raw = data.trim();
  if (!raw) return 0;
  const entries = raw.includes("\n") ? raw.split(/\r?\n/) : raw.split(/\s+/);
  return entries.filter((entry) => {
    const parts = splitAccountFields(entry);
    return (parts.length === 4 || parts.length === 5) && Boolean(parts[0]?.trim()) && Boolean(parts[2]?.trim()) && Boolean(parts[3]?.trim());
  }).length;
}

export function countGoogleAccounts(data: string): number {
  const raw = data.trim();
  if (!raw) return 0;
  const entries = raw.split(/\r?\n/);
  return entries.filter((entry) => {
    const parts = splitAccountFields(entry);
    return parts.length >= 2 && Boolean(parts[0]?.trim()) && Boolean(parts[1]?.trim());
  }).length;
}
