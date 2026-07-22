import { FormEvent, useCallback, useEffect, useMemo,  useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
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
  PlugZap,
  Server,
  Settings2,
  TestTube2,
  Users,
} from "lucide-react";

type JobStatus =
  | "running"
  | "stopping"
  | "completed"
  | "completed_with_errors"
  | "stopped";

type EmailSource = "duckmail" | "outlook" | "gmail" | "google";
type ActiveView = "overview" | "register" | "accounts" | "relay" | "logs";
type ExportFormat = "json" | "cpa";

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
  googleAccountCount?: number;
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
  relay?: RelayState;
}

interface RelayState {
  running: boolean;
  managed: boolean;
  healthy: boolean;
  returnCode?: number | null;
  lastLog?: string;
  config: {
    grok2apiPath: string;
    host: string;
    port: number;
    baseUrl: string;
    publicBaseUrl?: string;
    apiKey: string;
    apiKeyMasked: string;
    adminKey: string;
    adminKeyMasked: string;
    dataDir: string;
    logPath: string;
  };
}

interface ModelProbeRecord {
  id: string;
  name: string;
  capability: "chat" | "image" | "image_edit" | "video" | "unknown";
  status: "listed" | "ok" | "error";
  message: string;
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
  const [activeView, setActiveView] = useState<ActiveView>("overview");
  const [total, setTotal] = useState(5);
  const [concurrency, setConcurrency] = useState(1);
  const [oauthExchange, setOauthExchange] = useState(true);
  const [emailSource, setEmailSource] = useState<EmailSource>("duckmail");
  const [outlookData, setOutlookData] = useState("");
  const [googleData, setGoogleData] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  const [hideEmails, setHideEmails] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [refreshingQuota, setRefreshingQuota] = useState<Set<string>>(new Set());
  const [relayForm, setRelayForm] = useState({
    grok2apiPath: "",
    host: "127.0.0.1",
    port: 8000,
    apiKey: "local-grok-api-key",
    adminKey: "grok2api",
  });
  const [relayBusy, setRelayBusy] = useState<string | null>(null);
  const [relayFormReady, setRelayFormReady] = useState(false);
  const [syncMessage, setSyncMessage] = useState("");
  const [modelResults, setModelResults] = useState<ModelProbeRecord[]>([]);

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

  useEffect(() => {
    const relay = state.relay;
    if (!relay || relayFormReady) return;
    setRelayForm({
      grok2apiPath: relay.config.grok2apiPath,
      host: relay.config.host,
      port: relay.config.port,
      apiKey: relay.config.apiKey,
      adminKey: relay.config.adminKey,
    });
    setRelayFormReady(true);
  }, [relayFormReady, state.relay]);

  const job = state.job;
  const relay = state.relay;
  const relayPublicBaseUrl = relay?.config.publicBaseUrl || window.location.origin || relay?.config.baseUrl || "";
  const isRunning = job?.status === "running" || job?.status === "stopping";
  const relayRunning = Boolean(relay?.running);
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
  const googleAccountCount = useMemo(() => countGoogleAccounts(googleData), [googleData]);
  const needsGoogleAccounts = emailSource === "gmail" || emailSource === "google";
  const startDisabled =
    isRunning ||
    submitting ||
    (emailSource === "outlook" && outlookAccountCount === 0) ||
    (needsGoogleAccounts && googleAccountCount === 0);

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
          googleData: needsGoogleAccounts ? googleData : "",
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

  async function exportSelectedAccounts(format: ExportFormat) {
    if (selectedAccounts.size === 0) return;
    const exportKeys = selectedRows.map((account) => account.exportKey);
    if (exportKeys.length === 0) return;
    setExporting(format);
    setError(null);
    try {
      const endpoint = format === "cpa" ? "/api/accounts/export-cpa" : "/api/accounts/export";
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exportKeys }),
      });
      const contentType = response.headers.get("Content-Type") || "";
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error(`${format === "cpa" ? "CPA " : ""}导出接口不存在，请重启后端后再试`);
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
        (format === "cpa"
          ? `xai-cpa-credentials-${new Date().toISOString().replace(/[:.]/g, "-")}.zip`
          : `msdsj-grok-credentials-${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
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
      setExporting(null);
    }
  }

  async function saveRelayConfig(): Promise<boolean> {
    setRelayBusy("save");
    setError(null);
    try {
      const response = await apiJson<{ relay: RelayState }>("/api/relay/config", {
        method: "POST",
        body: JSON.stringify(relayForm),
      });
      setState((current) => ({ ...current, relay: response.relay }));
      setRelayForm({
        grok2apiPath: response.relay.config.grok2apiPath,
        host: response.relay.config.host,
        port: response.relay.config.port,
        apiKey: response.relay.config.apiKey,
        adminKey: response.relay.config.adminKey,
      });
      return true;
    } catch (configError) {
      setError(String(configError));
      return false;
    } finally {
      setRelayBusy(null);
    }
  }

  async function startRelay() {
    setRelayBusy("start");
    setError(null);
    try {
      const saved = await saveRelayConfig();
      if (!saved) return;
      setRelayBusy("start");
      const response = await apiJson<{ relay: RelayState }>("/api/relay/start", {
        method: "POST",
        body: "{}",
      });
      setState((current) => ({ ...current, relay: response.relay }));
    } catch (startError) {
      setError(String(startError));
    } finally {
      setRelayBusy(null);
    }
  }

  async function stopRelay() {
    setRelayBusy("stop");
    setError(null);
    try {
      const response = await apiJson<{ relay: RelayState }>("/api/relay/stop", {
        method: "POST",
        body: "{}",
      });
      setState((current) => ({ ...current, relay: response.relay }));
    } catch (stopError) {
      setError(String(stopError));
    } finally {
      setRelayBusy(null);
    }
  }

  async function syncRelayAccounts() {
    setRelayBusy("sync");
    setError(null);
    setSyncMessage("");
    try {
      const exportKeys = selectedRows.map((account) => account.exportKey);
      const response = await apiJson<{ sync: { requested: number; result: unknown }; relay: RelayState }>(
        "/api/relay/sync-accounts",
        {
          method: "POST",
          body: JSON.stringify({ exportKeys }),
        },
      );
      setState((current) => ({ ...current, relay: response.relay }));
      setSyncMessage(`已同步 ${response.sync.requested} 个账号到本地中转`);
    } catch (syncError) {
      setError(String(syncError));
    } finally {
      setRelayBusy(null);
    }
  }

  async function testRelayModels() {
    setRelayBusy("models");
    setError(null);
    setModelResults([]);
    try {
      const response = await apiJson<{ result: { models: ModelProbeRecord[] }; relay: RelayState }>(
        "/api/relay/models",
        {
          method: "POST",
          body: JSON.stringify({ probeChat: true }),
        },
      );
      setState((current) => ({ ...current, relay: response.relay }));
      setModelResults(response.result.models || []);
    } catch (modelsError) {
      setError(String(modelsError));
    } finally {
      setRelayBusy(null);
    }
  }

  const registrationPanel = (
    <section className="module-panel">
      <div className="panel-head">
        <div>
          <h2>注册任务</h2>
          <span>{job ? `任务 #${job.id.slice(0, 6)}` : "READY"}</span>
        </div>
        <span className={`status-pill ${statusTone(job?.status)}`}>
          {isRunning && <Loader2 size={14} className="spin" />}
          {statusLabel(job?.status)}
        </span>
      </div>
      <div className="module-body split-layout">
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
            <span>注册方式</span>
            <div className="segmented-control">
              <button type="button" className={emailSource === "duckmail" ? "active" : ""} disabled={isRunning} onClick={() => setEmailSource("duckmail")}>DuckMail</button>
              <button type="button" className={emailSource === "outlook" ? "active" : ""} disabled={isRunning} onClick={() => setEmailSource("outlook")}>Outlook</button>
              <button type="button" className={emailSource === "gmail" ? "active" : ""} disabled={isRunning} onClick={() => setEmailSource("gmail")}>Gmail 邮箱</button>
              <button type="button" className={emailSource === "google" ? "active" : ""} disabled={isRunning} onClick={() => setEmailSource("google")}>Google 账号</button>
            </div>
          </div>
          {emailSource === "outlook" && (
            <label className="textarea-field">
              <span>Outlook 账号池</span>
              <textarea value={outlookData} disabled={isRunning} placeholder="email----password----clientId----refreshToken" spellCheck={false} onChange={(event) => setOutlookData(event.target.value)} />
              <small>{outlookAccountCount > 0 ? `已识别 ${outlookAccountCount} 个邮箱` : "未识别到有效账号"}</small>
            </label>
          )}
          {needsGoogleAccounts && (
            <label className="textarea-field">
              <span>{emailSource === "google" ? "Google 账号池" : "Gmail 邮箱池"}</span>
              <textarea value={googleData} disabled={isRunning} placeholder={emailSource === "google" ? "email----password----recoveryEmail(可选)" : "email----appPassword"} spellCheck={false} onChange={(event) => setGoogleData(event.target.value)} />
              <small>{googleAccountCount > 0 ? `已识别 ${googleAccountCount} 个账号` : "未识别到有效账号"}</small>
            </label>
          )}
          <div className="action-row">
            <button className="primary-btn" type="submit" disabled={startDisabled}>
              {submitting && !isRunning ? <Loader2 size={18} className="spin" /> : <Play size={18} />}
              开始
            </button>
            <button className="secondary-btn" type="button" disabled={!isRunning || submitting} onClick={() => void stopRegistration()}>
              <Square size={17} />
              停止
            </button>
          </div>
        </form>
        <div className="job-card embedded">
          <div className="job-card-head">
            <span className={`status-pill ${statusTone(job?.status)}`}>{statusLabel(job?.status)}</span>
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
            <span>注册方式 {formatEmailSourceLabel(job)}</span>
          </div>
        </div>
      </div>
    </section>
  );

  const relayPanel = (
    <section className="relay-panel">
      <div className="panel-head">
        <div>
          <h2>统一 API 入口</h2>
          <span>{relayPublicBaseUrl || "等待配置"}</span>
        </div>
        <div className="panel-actions">
          <button className="icon-text-btn" type="button" disabled={!relayPublicBaseUrl} onClick={() => void copyText(relayPublicBaseUrl)}>
            <ClipboardCheck size={16} />
            复制 URL
          </button>
          <button className="icon-text-btn" type="button" disabled={!relay?.config.apiKey} onClick={() => void copyText(relay?.config.apiKey || "")}>
            <KeyRound size={16} />
            复制秘钥
          </button>
        </div>
      </div>
      <div className="relay-body">
        <div className="relay-config-grid">
          <label>
            <span>grok2api 路径</span>
            <input value={relayForm.grok2apiPath} spellCheck={false} onChange={(event) => setRelayForm((current) => ({ ...current, grok2apiPath: event.target.value }))} />
          </label>
          <label>
            <span>内部监听地址</span>
            <input value={relayForm.host} onChange={(event) => setRelayForm((current) => ({ ...current, host: event.target.value }))} />
          </label>
          <label>
            <span>内部端口</span>
            <input type="number" min={1} max={65535} value={relayForm.port} onChange={(event) => setRelayForm((current) => ({ ...current, port: Number(event.target.value) || 8000 }))} />
          </label>
          <label>
            <span>本地 API 秘钥</span>
            <input value={relayForm.apiKey} spellCheck={false} onChange={(event) => setRelayForm((current) => ({ ...current, apiKey: event.target.value }))} />
          </label>
          <label>
            <span>管理秘钥</span>
            <input value={relayForm.adminKey} spellCheck={false} onChange={(event) => setRelayForm((current) => ({ ...current, adminKey: event.target.value }))} />
          </label>
        </div>
        <div className="relay-actions wide">
          <button className="secondary-btn" type="button" disabled={relayBusy != null} onClick={() => void saveRelayConfig()}>
            {relayBusy === "save" ? <Loader2 size={16} className="spin" /> : <Settings2 size={16} />}
            保存
          </button>
          <button className="primary-btn" type="button" disabled={relayBusy != null || relayRunning} onClick={() => void startRelay()}>
            {relayBusy === "start" ? <Loader2 size={16} className="spin" /> : <Server size={16} />}
            启动
          </button>
          <button className="secondary-btn" type="button" disabled={relayBusy != null || !relayRunning} onClick={() => void stopRelay()}>
            <Square size={15} />
            停止
          </button>
        </div>
        <div className="endpoint-grid">
          <EndpointBox label="Base URL" value={relayPublicBaseUrl || "-"} />
          <EndpointBox label="Models" value={`${relayPublicBaseUrl || "-"}/v1/models`} />
          <EndpointBox label="Chat" value={`${relayPublicBaseUrl || "-"}/v1/chat/completions`} />
          <EndpointBox label="Bearer" value={relay?.config.apiKeyMasked || "-"} />
        </div>
        <div className="relay-tool-row">
          <button className="primary-btn" type="button" disabled={relayBusy != null} onClick={() => void syncRelayAccounts()}>
            {relayBusy === "sync" ? <Loader2 size={17} className="spin" /> : <PlugZap size={17} />}
            同步{selectedCount > 0 ? "选中" : "全部"}账号
          </button>
          <button className="secondary-btn" type="button" disabled={relayBusy != null || !relayRunning} onClick={() => void testRelayModels()}>
            {relayBusy === "models" ? <Loader2 size={17} className="spin" /> : <TestTube2 size={17} />}
            测试模型
          </button>
          {syncMessage && <span className="relay-message">{syncMessage}</span>}
        </div>
        <div className="curl-box">
          <code>{`curl ${relayPublicBaseUrl || "http://127.0.0.1:8765"}/v1/chat/completions -H "Authorization: Bearer ${relay?.config.apiKeyMasked || "local-grok-api-key"}"`}</code>
        </div>
        <div className="model-results">
          {modelResults.length === 0 ? (
            <div className="empty-row">尚未测试模型</div>
          ) : (
            modelResults.map((model) => (
              <div className={`model-row ${model.status}`} key={model.id}>
                <div>
                  <strong>{model.id}</strong>
                  <span>{model.name} / {capabilityLabel(model.capability)}</span>
                </div>
                <span className={`token-badge ${model.status === "ok" ? "ok" : model.status === "error" ? "missing" : ""}`}>
                  {model.status === "ok" ? "可调用" : model.status === "error" ? "失败" : "已列出"}
                </span>
                <p>{model.message}</p>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  );

  const logsPanel = (
    <section className="log-panel">
      <div className="panel-head">
        <div>
          <h2>任务日志</h2>
          <span>{job ? `启动 ${formatTime(job.startedAt)}` : "暂无任务"}</span>
        </div>
      </div>
      <div className="log-list tall">
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
  );

  const accountsPanel = (
    <section className="accounts-panel">
      <div className="panel-head">
        <div>
          <h2>账号列表</h2>
          <span>{loading ? "加载中" : selectedCount > 0 ? `已选择 ${selectedCount} / ${accountsWithKeys.length}` : `${accountsWithKeys.length} 个账号`}</span>
        </div>
        <div className="panel-actions">
          <button className="export-btn" type="button" disabled={selectedCount === 0 || exporting !== null} onClick={() => void exportSelectedAccounts("json")}>
            {exporting === "json" ? <Loader2 size={16} className="spin" /> : <Download size={16} />}
            导出 JSON
          </button>
          <button className="export-btn cpa-export-btn" type="button" disabled={selectedCount === 0 || exporting !== null} onClick={() => void exportSelectedAccounts("cpa")}>
            {exporting === "cpa" ? <Loader2 size={16} className="spin" /> : <KeyRound size={16} />}
            导出 CPA
          </button>
          <button className="small-icon-btn" type="button" onClick={() => void refresh()} aria-label="刷新账号列表" title="刷新账号列表">
            <RefreshCw size={16} />
          </button>
        </div>
      </div>
      <div className="accounts-table-wrap">
        <table className="accounts-table">
          <thead>
            <tr>
              <th className="select-col"><input type="checkbox" checked={allSelected} disabled={accountsWithKeys.length === 0} aria-label="选择全部账号" onChange={toggleAllAccounts} /></th>
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
              <tr><td colSpan={8}><div className="empty-row">暂无账号</div></td></tr>
            ) : (
              accountsWithKeys.map((account) => (
                <tr key={account.rowKey}>
                  <td className="select-col"><input type="checkbox" checked={selectedAccounts.has(account.rowKey)} aria-label={`选择 ${maskEmail(account.email)}`} onChange={() => toggleAccount(account.rowKey)} /></td>
                  <td><div className="email-cell"><strong>{hideEmails ? maskEmail(account.email) : account.email}</strong>{account.error && <span>{account.error}</span>}</div></td>
                  <td><span className={`token-badge ${account.hasRefreshToken ? "ok" : "missing"}`}>{account.hasRefreshToken && <ShieldCheck size={13} />}{account.hasRefreshToken ? "已获取" : "缺失"}</span></td>
                  <td>{account.displayName || account.userId || "-"}</td>
                  <td>{account.planType || "-"}</td>
                  <td><QuotaCell quota={account.quota} /></td>
                  <td>{account.createdAtLabel || "-"}</td>
                  <td>
                    <button className="small-icon-btn" type="button" title="刷新额度" disabled={refreshingQuota.has(account.id)} onClick={() => void refreshQuota(account.id)}>
                      {refreshingQuota.has(account.id) ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );

  const overviewPanel = (
    <>
      <div className="stats-grid">
        <MetricCard icon={<Users size={20} />} label="账号总数" value={state.accounts.length} tone="teal" />
        <MetricCard icon={<KeyRound size={20} />} label="Refresh Token" value={refreshReadyCount} tone="indigo" />
        <MetricCard icon={<CheckCircle2 size={20} />} label="本次成功" value={job?.completed ?? 0} tone="green" />
        <MetricCard icon={<Server size={20} />} label="本地中转" value={relayRunning ? "运行中" : "未启动"} text tone="amber" />
      </div>
      {registrationPanel}
      {relayPanel}
      {logsPanel}
    </>
  );

  const activePanel = {
    overview: overviewPanel,
    register: registrationPanel,
    accounts: accountsPanel,
    relay: relayPanel,
    logs: logsPanel,
  }[activeView];

  return (
    <main className="app-shell admin-shell">
      <aside className="app-sidebar">
        <div className="sidebar-brand">
          <div className="brand-mark" aria-hidden="true"><Cpu size={22} /></div>
          <div>
            <span className="eyebrow">grok-account-manager</span>
            <strong>MSDSJ Grok</strong>
          </div>
        </div>
        <nav className="sidebar-nav">
          <NavButton icon={<Activity size={17} />} label="总览" active={activeView === "overview"} onClick={() => setActiveView("overview")} />
          <NavButton icon={<Play size={17} />} label="注册任务" active={activeView === "register"} onClick={() => setActiveView("register")} />
          <NavButton icon={<Users size={17} />} label="账号列表" active={activeView === "accounts"} onClick={() => setActiveView("accounts")} />
          <NavButton icon={<Server size={17} />} label="本地中转" active={activeView === "relay"} onClick={() => setActiveView("relay")} />
          <NavButton icon={<FolderOpen size={17} />} label="任务日志" active={activeView === "logs"} onClick={() => setActiveView("logs")} />
        </nav>
        <div className="sidebar-status">
          <span className={`status-pill ${statusTone(job?.status)}`}>{statusLabel(job?.status)}</span>
          <span className={`status-pill ${relayRunning ? "success" : "neutral"}`}>{relayRunning ? "中转运行" : "中转未启动"}</span>
        </div>
      </aside>

      <section className="app-content">
        <header className="topbar admin-topbar">
          <div>
            <span className="eyebrow">CONTROL PANEL</span>
            <h1>{viewTitle(activeView)}</h1>
            <p>{viewSubtitle(activeView)}</p>
          </div>
          <div className="topbar-actions">
            <button className="icon-text-btn" type="button" onClick={() => void refresh()}><RefreshCw size={17} />刷新</button>
            <button className="icon-text-btn" type="button" onClick={() => setHideEmails((current) => !current)}>
              {hideEmails ? <Eye size={17} /> : <EyeOff size={17} />}
              {hideEmails ? "显示邮箱" : "隐藏邮箱"}
            </button>
          </div>
        </header>

        {error && <div className="notice error"><AlertTriangle size={18} /><span>{error}</span></div>}

        <section className="main-panel">{activePanel}</section>
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

function countGoogleAccounts(data: string): number {
  const raw = String(data || "").trim();
  if (!raw) return 0;
  return raw.split(/\r?\n/).filter((entry) => {
    const parts = entry.split(/-{4,}/);
    return parts.length >= 2 && Boolean(parts[0]?.trim()) && Boolean(parts[1]?.trim());
  }).length;
}

function formatEmailSourceLabel(job: RegistrationJob | null): string {
  switch (job?.emailSource) {
    case "outlook":
      return `Outlook(${job.outlookAccountCount ?? 0})`;
    case "gmail":
      return `Gmail 邮箱(${job.googleAccountCount ?? 0})`;
    case "google":
      return `Google 账号(${job.googleAccountCount ?? 0})`;
    case "duckmail":
      return "DuckMail";
    default:
      return "DuckMail";
  }
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

function EndpointBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="endpoint-box">
      <span>{label}</span>
      <code>{value}</code>
    </div>
  );
}

function NavButton({
  icon,
  label,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`nav-button ${active ? "active" : ""}`} type="button" onClick={onClick}>
      {icon}
      <span>{label}</span>
    </button>
  );
}

function viewTitle(view: ActiveView): string {
  switch (view) {
    case "register":
      return "注册任务";
    case "accounts":
      return "账号列表";
    case "relay":
      return "统一 API 入口";
    case "logs":
      return "任务日志";
    default:
      return "系统总览";
  }
}

function viewSubtitle(view: ActiveView): string {
  switch (view) {
    case "register":
      return "创建账号、控制并发和查看任务进度";
    case "accounts":
      return "查看、选择、导出和刷新本地凭据";
    case "relay":
      return "前端和 OpenAI 兼容接口共用当前后端端口";
    case "logs":
      return "查看最近任务事件和失败信息";
    default:
      return "任务状态、账号资产和本地中转概览";
  }
}

function capabilityLabel(value: ModelProbeRecord["capability"]): string {
  switch (value) {
    case "chat":
      return "对话";
    case "image":
      return "生图";
    case "image_edit":
      return "改图";
    case "video":
      return "视频";
    default:
      return "未知";
  }
}

async function copyText(value: string) {
  if (!value || value === "-") return;
  await navigator.clipboard.writeText(value);
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
