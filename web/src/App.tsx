import { FormEvent, useCallback, useEffect, useMemo,  useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Download,
  FolderOpen,
  KeyRound,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Eye,
  EyeOff,
  Users,
} from "lucide-react";

type JobStatus =
  | "running"
  | "stopping"
  | "completed"
  | "completed_with_errors"
  | "stopped";

type EmailSource = "duckmail" | "outlook";

interface JobEvent {
  id: string;
  time: number;
  level: "info" | "success" | "warning" | "error";
  message: string;
  worker?: number;
  round?: number;
  email?: string;
}

interface RegistrationJob {
  id: string;
  status: JobStatus;
  total: number;
  concurrency: number;
  oauthExchange: boolean;
  emailSource?: EmailSource;
  outlookAccountCount?: number;
  issued: number;
  completed: number;
  failed: number;
  workerErrors: number;
  active: number;
  startedAt: number;
  finishedAt: number | null;
  events: JobEvent[];
}

interface AccountRecord {
  id: string;
  exportKey?: string;
  email: string;
  displayName: string;
  authMode: string;
  planType: string;
  userId: string;
  createdAt: number;
  createdAtLabel: string;
  hasRefreshToken: boolean;
  hasAccessToken: boolean;
  fileName: string;
  filePath: string;
  error?: string;
  quota?: {
    frequentUsage?: number | null;
    frequentLimit?: number | null;
    occasionalUsage?: number | null;
    occasionalLimit?: number | null;
    weeklyUsed?: number | null;
    weeklyTotal?: number | null;
    weeklyLimitPercent?: number | null;
  } | null;
  usageUpdatedAt?: number;
}

interface AppState {
  job: RegistrationJob | null;
  accounts: AccountRecord[];
}

async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const data = (await response.json()) as T & { error?: string };
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function formatTime(value: number | null | undefined): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function statusLabel(status?: JobStatus): string {
  switch (status) {
    case "running":
      return "运行中";
    case "stopping":
      return "停止中";
    case "completed":
      return "已完成";
    case "completed_with_errors":
      return "有失败";
    case "stopped":
      return "已停止";
    default:
      return "空闲";
  }
}

function statusTone(status?: JobStatus): string {
  if (status === "running") return "running";
  if (status === "completed") return "success";
  if (status === "completed_with_errors") return "warning";
  if (status === "stopping" || status === "stopped") return "neutral";
  return "neutral";
}

export function App() {
  const [state, setState] = useState<AppState>({ job: null, accounts: [] });
  const [total, setTotal] = useState(5);
  const [concurrency, setConcurrency] = useState(1);
  const [oauthExchange, setOauthExchange] = useState(true);
  const [emailSource, setEmailSource] = useState<EmailSource>("duckmail");
  const [outlookData, setOutlookData] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  const [hideEmails, setHideEmails] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [refreshingQuota, setRefreshingQuota] = useState<Set<string>>(new Set());

  async function refreshQuota(accountId: string) {
    setRefreshingQuota((s) => new Set(s).add(accountId));
    try {
      await apiJson("/api/accounts/refresh-quota", {
        method: "POST",
        body: JSON.stringify({ accountId }),
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setRefreshingQuota((s) => { const n = new Set(s); n.delete(accountId); return n; });
    }
  }

  const refresh = useCallback(async () => {
    const next = await apiJson<AppState>("/api/state");
    setState(next);
  }, []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const next = await apiJson<AppState>("/api/state");
        if (alive) setState(next);
      } catch (loadError) {
        if (alive) setError(String(loadError));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const job = state.job;
  const isRunning = job?.status === "running" || job?.status === "stopping";
  const finished = job ? job.completed + job.failed : 0;
  const progress = job && job.total > 0 ? Math.round((finished / job.total) * 100) : 0;
  const refreshReadyCount = useMemo(
    () => state.accounts.filter((account) => account.hasRefreshToken).length,
    [state.accounts],
  );
  const accountsWithKeys = useMemo(
    () =>
      state.accounts.map((account, index) => ({
        ...account,
        rowKey: getAccountRowKey(account, index),
        exportKey: getAccountExportKey(account, index),
      })),
    [state.accounts],
  );
  const selectedRows = useMemo(
    () => accountsWithKeys.filter((account) => selectedAccounts.has(account.rowKey)),
    [accountsWithKeys, selectedAccounts],
  );
  const selectedCount = selectedRows.length;
  const allSelected =
    accountsWithKeys.length > 0 &&
    accountsWithKeys.every((account) => selectedAccounts.has(account.rowKey));
  const outlookAccountCount = useMemo(() => countOutlookAccounts(outlookData), [outlookData]);
  const startDisabled =
    isRunning || submitting || (emailSource === "outlook" && outlookAccountCount === 0);

  const latestEvents = useMemo(() => {
    return [...(job?.events || [])].reverse().slice(0, 80);
  }, [job?.events]);

  useEffect(() => {
    setSelectedAccounts((current) => {
      const available = new Set(accountsWithKeys.map((account) => account.rowKey));
      const next = new Set([...current].filter((key) => available.has(key)));
      return next.size === current.size ? current : next;
    });
  }, [accountsWithKeys]);

  async function startRegistration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const safeConcurrency = Math.max(1, Math.min(12, Number(concurrency) || 1));
    setConcurrency(safeConcurrency);
    setSubmitting(true);
    setError(null);
    try {
      const response = await apiJson<{ job: RegistrationJob }>("/api/register", {
        method: "POST",
        body: JSON.stringify({
          total,
          concurrency: safeConcurrency,
          oauthExchange,
          emailSource,
          outlookData: emailSource === "outlook" ? outlookData : "",
        }),
      });
      setState((current) => ({ ...current, job: response.job }));
    } catch (startError) {
      setError(String(startError));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopRegistration() {
    setSubmitting(true);
    setError(null);
    setState((current) => {
      if (!current.job || current.job.status !== "running") return current;
      return { ...current, job: { ...current.job, status: "stopping" } };
    });
    try {
      const response = await apiJson<{ job: RegistrationJob | null }>(
        "/api/register/stop",
        { method: "POST", body: "{}" },
      );
      setState((current) => ({ ...current, job: response.job }));
    } catch (stopError) {
      setError(String(stopError));
    } finally {
      setSubmitting(false);
    }
  }

  function toggleAccount(rowKey: string) {
    setSelectedAccounts((current) => {
      const next = new Set(current);
      if (next.has(rowKey)) {
        next.delete(rowKey);
      } else {
        next.add(rowKey);
      }
      return next;
    });
  }

  function toggleAllAccounts() {
    setSelectedAccounts((current) => {
      if (accountsWithKeys.length === 0) return current;
      if (accountsWithKeys.every((account) => current.has(account.rowKey))) {
        return new Set();
      }
      return new Set(accountsWithKeys.map((account) => account.rowKey));
    });
  }

  async function exportSelectedAccounts() {
    if (selectedAccounts.size === 0) return;
    const exportKeys = selectedRows.map((account) => account.exportKey);
    if (exportKeys.length === 0) return;
    setExporting(true);
    setError(null);
    try {
      const response = await fetch("/api/accounts/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exportKeys }),
      });
      const contentType = response.headers.get("Content-Type") || "";
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error("导出接口不存在，请重启后端 grok-account-manager-web 后再试");
        }
        if (contentType.includes("application/json")) {
          const data = (await response.json()) as { error?: string };
          throw new Error(data.error || `HTTP ${response.status}`);
        }
        throw new Error(`HTTP ${response.status}`);
      }

      const blob = await response.blob();
      const contentDisposition = response.headers.get("Content-Disposition") || "";
      const filenameMatch = contentDisposition.match(/filename="([^"]+)"/);
      const filename =
        filenameMatch?.[1] ||
        `msdsj-grok-credentials-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setError(String(exportError));
    } finally {
      setExporting(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            <Cpu size={22} />
          </div>
          <div>
            <span className="eyebrow">grok-account-manager</span>
            <h1>MSDSJ Grok 注册机</h1>
            <p>任务运行 / Token 状态 / 凭据归档</p>
          </div>
        </div>
        <div className="topbar-actions">
          <span className={`system-chip ${statusTone(job?.status)}`}>
            {isRunning ? <Loader2 size={15} className="spin" /> : <Activity size={15} />}
            {statusLabel(job?.status)}
          </span>
          <button className="icon-text-btn" type="button" onClick={() => void refresh()}>
            <RefreshCw size={17} />
            刷新
          </button>
          <button
            className="icon-text-btn"
            type="button"
            onClick={() => setHideEmails((current) => !current)}
          >
            {hideEmails ? <Eye size={17} /> : <EyeOff size={17} />}
            {hideEmails ? "显示邮箱" : "隐藏邮箱"}
          </button>
        </div>
      </header>

      {error && (
        <div className="notice error">
          <AlertTriangle size={18} />
          <span>{error}</span>
        </div>
      )}

      <section className="workspace">
        <aside className="control-panel">
          <div className="control-head">
            <div>
              <span className="section-kicker">CONTROL</span>
              <h2>注册任务</h2>
            </div>
            <span className="job-id">{job ? `#${job.id.slice(0, 6)}` : "READY"}</span>
          </div>
          <form className="run-form" onSubmit={(event) => void startRegistration(event)}>
            <label>
              <span>注册次数</span>
              <input
                type="number"
                min={1}
                max={10000}
                value={total}
                disabled={isRunning}
                onChange={(event) => setTotal(Number(event.target.value))}
              />
            </label>

            <label>
              <span>并发账号</span>
              <input
                type="number"
                min={1}
                max={20}
                value={concurrency}
                disabled={isRunning}
                onChange={(event) =>
                  setConcurrency(Math.max(1, Math.min(20, Number(event.target.value) || 1)))
                }
              />
            </label>

            <label className="toggle-row">
              <input
                type="checkbox"
                checked={oauthExchange}
                disabled={isRunning}
                onChange={(event) => setOauthExchange(event.target.checked)}
              />
              <span>获取 refresh_token</span>
            </label>

            <div className="field-group">
              <span>邮箱源</span>
              <div className="segmented-control">
                <button
                  type="button"
                  className={emailSource === "duckmail" ? "active" : ""}
                  disabled={isRunning}
                  onClick={() => setEmailSource("duckmail")}
                >
                  DuckMail
                </button>
                <button
                  type="button"
                  className={emailSource === "outlook" ? "active" : ""}
                  disabled={isRunning}
                  onClick={() => setEmailSource("outlook")}
                >
                  Outlook
                </button>
              </div>
            </div>

            {emailSource === "outlook" && (
              <label className="textarea-field">
                <span>Outlook 账号池</span>
                <textarea
                  value={outlookData}
                  disabled={isRunning}
                  placeholder="email----password----clientId----refreshToken"
                  spellCheck={false}
                  onChange={(event) => setOutlookData(event.target.value)}
                />
                <small>{outlookAccountCount > 0 ? `已识别 ${outlookAccountCount} 个邮箱` : "未识别到有效账号"}</small>
              </label>
            )}

            <div className="action-row">
              <button
                className="primary-btn"
                type="submit"
                disabled={startDisabled}
              >
                {submitting && !isRunning ? <Loader2 size={18} className="spin" /> : <Play size={18} />}
                开始
              </button>
              <button
                className="secondary-btn"
                type="button"
                disabled={!isRunning || submitting}
                onClick={() => void stopRegistration()}
              >
                <Square size={17} />
                停止
              </button>
            </div>
          </form>

          <div className="job-card">
            <div className="job-card-head">
              <span className={`status-pill ${statusTone(job?.status)}`}>
                {isRunning && <Loader2 size={14} className="spin" />}
                {statusLabel(job?.status)}
              </span>
              <strong>{job ? `${finished}/${job.total}` : "0/0"}</strong>
            </div>
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${progress}%` }} />
            </div>
            <div className="progress-meta">
              <span>当前进度</span>
              <strong>{progress}%</strong>
            </div>
            <div className="job-metrics">
              <span>已发起 {job?.issued ?? 0}</span>
              <span>成功 {job?.completed ?? 0}</span>
              <span>失败 {job?.failed ?? 0}</span>
              <span>活跃 {job?.active ?? 0}</span>
              <span>启动异常 {job?.workerErrors ?? 0}</span>
              <span>邮箱源 {job?.emailSource === "outlook" ? `Outlook(${job.outlookAccountCount ?? 0})` : "DuckMail"}</span>
            </div>
          </div>
        </aside>

        <section className="main-panel">
          <div className="stats-grid">
            <MetricCard icon={<Users size={20} />} label="账号总数" value={state.accounts.length} tone="teal" />
            <MetricCard icon={<KeyRound size={20} />} label="Refresh Token" value={refreshReadyCount} tone="indigo" />
            <MetricCard icon={<CheckCircle2 size={20} />} label="本次成功" value={job?.completed ?? 0} tone="green" />
            <MetricCard icon={<FolderOpen size={20} />} label="输出目录" value="output/credentials" text tone="amber" />
          </div>

          <section className="log-panel">
            <div className="panel-head">
              <div>
                <h2>任务日志</h2>
                <span>{job ? `启动 ${formatTime(job.startedAt)}` : "暂无任务"}</span>
              </div>
            </div>
            <div className="log-list">
                  {latestEvents.length === 0 ? (
                <div className="empty-row">等待任务启动</div>
              ) : (
                latestEvents.map((event) => (
                  <div className={`log-row ${event.level}`} key={event.id}>
                    <span>{formatTime(event.time)}</span>
                    <strong>{event.level}</strong>
                    <p>{hideEmails ? maskTextEmails(event.message) : event.message}</p>
                  </div>
                ))
              )}
            </div>
          </section>

          <section className="accounts-panel">
            <div className="panel-head">
              <div>
                <h2>账号列表</h2>
                <span>
                  {loading
                    ? "加载中"
                    : selectedCount > 0
                      ? `已选择 ${selectedCount} / ${accountsWithKeys.length}`
                      : `${accountsWithKeys.length} 个账号`}
                </span>
              </div>
              <div className="panel-actions">
                <button
                  className="export-btn"
                  type="button"
                  disabled={selectedCount === 0 || exporting}
                  onClick={() => void exportSelectedAccounts()}
                >
                  {exporting ? <Loader2 size={16} className="spin" /> : <Download size={16} />}
                  导出 JSON
                </button>
                <button
                  className="small-icon-btn"
                  type="button"
                  onClick={() => void refresh()}
                  aria-label="刷新账号列表"
                  title="刷新账号列表"
                >
                  <RefreshCw size={16} />
                </button>
              </div>
            </div>
            <div className="accounts-table-wrap">
              <table className="accounts-table">
                <thead>
                  <tr>
                    <th className="select-col">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        disabled={accountsWithKeys.length === 0}
                        aria-label="选择全部账号"
                        onChange={toggleAllAccounts}
                      />
                    </th>
                    <th>邮箱</th>
                    <th>Refresh</th>
                    <th>用户</th>
                    <th>套餐</th>
                    <th>额度</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {accountsWithKeys.length === 0 ? (
                    <tr>
                      <td colSpan={7}>
                        <div className="empty-row">暂无账号</div>
                      </td>
                    </tr>
                  ) : (
                    accountsWithKeys.map((account) => (
                      <tr key={account.rowKey}>
                        <td className="select-col">
                          <input
                            type="checkbox"
                            checked={selectedAccounts.has(account.rowKey)}
                            aria-label={`选择 ${maskEmail(account.email)}`}
                            onChange={() => toggleAccount(account.rowKey)}
                          />
                        </td>
                        <td>
                          <div className="email-cell">
                            <strong>{hideEmails ? maskEmail(account.email) : account.email}</strong>
                            {account.error && <span>{account.error}</span>}
                          </div>
                        </td>
                        <td>
                          <span className={`token-badge ${account.hasRefreshToken ? "ok" : "missing"}`}>
                            {account.hasRefreshToken && <ShieldCheck size={13} />}
                            {account.hasRefreshToken ? "已获取" : "缺失"}
                          </span>
                        </td>
                        <td>{account.displayName || account.userId || "-"}</td>
                        <td>{account.planType || "-"}</td>
                        <td>
                          <QuotaCell quota={account.quota} />
                        </td>
                        <td>{account.createdAtLabel || "-"}</td>
                        <td>
                          <button
                            className="small-icon-btn"
                            type="button"
                            title="刷新额度"
                            disabled={refreshingQuota.has(account.id)}
                            onClick={() => void refreshQuota(account.id)}
                          >
                            {refreshingQuota.has(account.id)
                              ? <Loader2 size={14} className="spin" />
                              : <RefreshCw size={14} />}
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </section>
      </section>
    </main>
  );
}

function getAccountRowKey(account: AccountRecord, index: number): string {
  return [
    account.exportKey || "",
    account.fileName || "",
    account.id || "",
    account.email || "",
    String(index),
  ].join("|");
}

function getAccountExportKey(account: AccountRecord, index: number): string {
  if (account.exportKey) return account.exportKey;
  return account.fileName ? `${account.fileName}:0` : `${account.id || index}:0`;
}

function maskEmail(email: string): string {
  const normalized = String(email || "").trim();
  const atIndex = normalized.indexOf("@");
  if (atIndex <= 0) return normalized || "-";

  const name = normalized.slice(0, atIndex);
  const domain = normalized.slice(atIndex + 1);
  const dotIndex = domain.lastIndexOf(".");
  const domainName = dotIndex > 0 ? domain.slice(0, dotIndex) : domain;
  const suffix = dotIndex > 0 ? domain.slice(dotIndex + 1) : "";
  return `${maskPart(name, 2)}@${maskPart(domainName, 1)}${suffix ? `.${maskPart(suffix, 0)}` : ""}`;
}

function maskPart(value: string, visible: number): string {
  if (!value) return "***";
  const left = value.slice(0, Math.min(visible, value.length));
  return `${left}${"*".repeat(Math.max(3, value.length - left.length))}`;
}

function maskTextEmails(text: string): string {
  return String(text || "").replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, (email) =>
    maskEmail(email),
  );
}

function countOutlookAccounts(data: string): number {
  const raw = String(data || "").trim();
  if (!raw) return 0;
  const entries = raw.includes("\n") ? raw.split(/\r?\n/) : raw.split(/\s+/);
  return entries.filter((entry) => entry.split(/-{4,}/).length === 4).length;
}

function QuotaCell({ quota }: { quota: AccountRecord["quota"] }) {
  if (!quota) return <span style={{ color: "var(--text-muted)" }}>-</span>;
  const rows: string[] = [];
  if (quota.frequentLimit != null)
    rows.push(`高频 ${quota.frequentUsage ?? 0}/${quota.frequentLimit}`);
  if (quota.occasionalLimit != null)
    rows.push(`普通 ${quota.occasionalUsage ?? 0}/${quota.occasionalLimit}`);
  if (quota.weeklyTotal != null)
    rows.push(`周 ${quota.weeklyUsed ?? 0}/${quota.weeklyTotal}`);
  if (rows.length === 0) return <span style={{ color: "var(--text-muted)" }}>-</span>;
  return (
    <div style={{ fontSize: "0.78rem", lineHeight: 1.6 }}>
      {rows.map((r) => <div key={r}>{r}</div>)}
    </div>
  );
}

function MetricCard({
  icon,
  label,
  value,
  text = false,
  tone = "teal",
}: {
  icon: React.ReactNode;
  label: string;
  value: number | string;
  text?: boolean;
  tone?: "teal" | "indigo" | "green" | "amber";
}) {
  return (
    <div className={`metric-card ${tone}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong className={text ? "metric-text" : ""}>{value}</strong>
    </div>
  );
}
