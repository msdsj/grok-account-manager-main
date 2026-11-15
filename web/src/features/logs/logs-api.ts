export type OperationLog = {
  id: number;
  actor: string;
  action: string;
  targetType: string;
  targetId: string;
  detail: Record<string, unknown>;
  result: string;
  createdAt: number;
};

export type JobLog = {
  id: number;
  level: string;
  stage: string;
  message: string;
  worker: number;
  payload: Record<string, unknown>;
  createdAt: number;
};

export type SystemLog = {
  id: number;
  level: string;
  source: string;
  message: string;
  detail: Record<string, unknown>;
  createdAt: number;
};

async function api<T>(path: string): Promise<T> {
  const response = await fetch(path);
  const payload = (await response.json().catch(() => ({}))) as T & { error?: string };
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

export function listOperationLogs(input: {
  actor?: string;
  action?: string;
  result?: string;
  limit?: number;
  offset?: number;
}): Promise<{ items: OperationLog[]; total: number }> {
  const query = new URLSearchParams();
  if (input.actor) query.set("actor", input.actor);
  if (input.action) query.set("action", input.action);
  if (input.result) query.set("result", input.result);
  query.set("limit", String(input.limit ?? 100));
  query.set("offset", String(input.offset ?? 0));
  return api<{ items: OperationLog[]; total: number }>(`/api/logs/operations?${query.toString()}`);
}

export function listJobLogs(
  jobId: string,
  input: { limit?: number; offset?: number } = {},
): Promise<{ items: JobLog[]; total: number }> {
  const query = new URLSearchParams({
    limit: String(input.limit ?? 500),
    offset: String(input.offset ?? 0),
  });
  return api<{ items: JobLog[]; total: number }>(`/api/logs/jobs/${encodeURIComponent(jobId)}?${query.toString()}`);
}

export function listSystemLogs(input: {
  level?: string;
  source?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<{ items: SystemLog[]; total: number }> {
  const query = new URLSearchParams();
  if (input.level) query.set("level", input.level);
  if (input.source) query.set("source", input.source);
  query.set("limit", String(input.limit ?? 200));
  query.set("offset", String(input.offset ?? 0));
  return api<{ items: SystemLog[]; total: number }>(`/api/logs/system?${query.toString()}`);
}
