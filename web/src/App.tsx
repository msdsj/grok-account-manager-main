import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Copy,
  Cpu,
  Download,
  ExternalLink,
  Github,
  ImageIcon,
  KeyRound,
  Loader2,
  Megaphone,
  MessageCircle,
  Play,
  RefreshCw,
  ShieldCheck,
  Send,
  Sparkles,
  Square,
  Star,
  Eye,
  EyeOff,
  PlugZap,
  Server,
  Settings2,
  Terminal,
  TestTube2,
  Trash2,
  Users,
  X,
} from "lucide-react";
import { appRoutes, routeForPath, routeForView, type ActiveView } from "./routes";

type JobStatus =
  | "running"
  | "stopping"
  | "completed"
  | "completed_with_errors"
  | "stopped";

type EmailSource = "duckmail" | "outlook" | "gmail" | "google";
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

interface FailedAccount {
  id: string;
  time: number;
  email: string;
  round: number;
  worker: number;
  stage: string;
  reason: string;
  timedOut: boolean;
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
  roundTimeoutSeconds?: number;
  failedAccounts?: FailedAccount[];
  startedAt: number;
  finishedAt: number | null;
  events: JobEvent[];
}

interface AccountAvailability {
  category: "cli-4.5" | "grok-4.5" | "chat-image" | "base-only" | "image-only" | "unavailable";
  baseAvailable: boolean;
  chatAvailable?: boolean;
  cli45Available?: boolean;
  grok45Available: boolean;
  imageAvailable?: boolean;
  baseModel?: string | null;
  chatModel?: string | null;
  cli45Model?: string | null;
  grok45Model?: string | null;
  imageModel?: string | null;
  imageSource?: string | null;
  latencyMs?: number | null;
  error?: string | null;
  testedAt?: number | null;
}

interface AccountRecord {
  id: string;
  exportKey?: string;
  email: string;
  displayName: string;
  authMode: string;
  planType: string;
  hasGrokCodeAccess?: boolean | null;
  userId: string;
  createdAt: number;
  createdAtLabel: string;
  hasRefreshToken: boolean;
  hasAccessToken: boolean;
  fileName: string;
  filePath: string;
  error?: string;
  availability?: AccountAvailability;
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

interface AccountTestResult extends AccountAvailability {
  id?: string;
  exportKey?: string;
  email: string;
  fileName?: string;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  error?: string;
}

interface GeneratedImage {
  url?: string;
  b64_json?: string;
  revised_prompt?: string;
}

const CHAT_TEST_MODELS = [
  "grok-4.5",
  "grok-4.20-auto",
  "grok-4.20-fast",
  "grok-4.20-expert",
  "grok-4.20-0309",
  "grok-4.20-0309-reasoning",
  "grok-4.3-beta",
];

const IMAGE_TEST_MODELS = [
  "grok-imagine-image-lite",
  "grok-imagine-image",
  "grok-imagine-image-pro",
];

const IMAGE_SIZES = ["1024x1024", "1792x1024", "1024x1792", "1280x720", "720x1280"];
const PROJECT_GITHUB_URL = "https://github.com/msdsj/grok-account-manager-main";
const QQ_GROUP_NUMBER = "972295238";
const COMMUNITY_QR_SRC = "/community-qr.png";
const ANNOUNCEMENT_VERSION = "2026-07-23-ui-community";
const ANNOUNCEMENT_ITEMS = [
  "补充 CHANGELOG.md 更新日志，整理 FastAPI 重构、账号数据库、Grok CLI 4.5、Chat 对话、图片生成和本地中转改动。",
  "新增项目更新公告弹窗，后续每次版本更新都会在这里追加一条说明。",
  "首页新增 QQ 交流群入口，群号 972295238，方便集中反馈账号池、CLI 4.5 和图片生成问题。",
  "欢迎到 GitHub 项目页点 Star，后续版本会继续围绕账号池测试和本地中转稳定性优化。",
];

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
  const [activeView, setActiveView] = useState<ActiveView>(() => routeForPath(window.location.pathname).id);
  const [total, setTotal] = useState(5);
  const [concurrency, setConcurrency] = useState(1);
  const [oauthExchange, setOauthExchange] = useState(true);
  const [emailSource, setEmailSource] = useState<EmailSource>("duckmail");
  const [outlookData, setOutlookData] = useState("");
  const [googleData, setGoogleData] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  const [testingAccounts, setTestingAccounts] = useState(false);
  const [refreshingSelected, setRefreshingSelected] = useState(false);
  const [accountTestResults, setAccountTestResults] = useState<AccountTestResult[]>([]);
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  const [activeTestAccountKey, setActiveTestAccountKey] = useState("");
  const [hideEmails, setHideEmails] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [announcementOpen, setAnnouncementOpen] = useState(true);

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
  const [chatModel, setChatModel] = useState("grok-4.5");
  const [chatPrompt, setChatPrompt] = useState("用一句话回复 OK，并说明当前模型可用。");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [imageModel, setImageModel] = useState("grok-imagine-image-lite");
  const [imagePrompt, setImagePrompt] = useState("一张干净明亮的小清新控制台界面，七彩玻璃泡泡点缀，柔和自然光");
  const [imageSize, setImageSize] = useState("1024x1024");
  const [imageCount, setImageCount] = useState(1);
  const [imageBusy, setImageBusy] = useState(false);
  const [generatedImages, setGeneratedImages] = useState<GeneratedImage[]>([]);

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

  const navigateTo = useCallback((view: ActiveView) => {
    const route = routeForView(view);
    setActiveView(route.id);
    if (window.location.pathname !== route.path) {
      window.history.pushState({ view: route.id }, "", route.path);
    }
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
    const onPopState = () => setActiveView(routeForPath(window.location.pathname).id);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
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

  const closeAnnouncement = useCallback(() => {
    setAnnouncementOpen(false);
  }, []);

  useEffect(() => {
    if (!announcementOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeAnnouncement();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [announcementOpen, closeAnnouncement]);

  const job = state.job;
  const relay = state.relay;
  const activeRoute = routeForView(activeView);
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
  const activeTestAccount = useMemo(
    () => accountsWithKeys.find((account) => account.rowKey === activeTestAccountKey) || accountsWithKeys[0] || null,
    [accountsWithKeys, activeTestAccountKey],
  );
  const selectedCount = selectedRows.length;
  const allSelected =
    accountsWithKeys.length > 0 &&
    accountsWithKeys.every((account) => selectedAccounts.has(account.rowKey));
  const cli45Count = useMemo(
    () => state.accounts.filter((account) => hasCli45Capability(account.availability)).length,
    [state.accounts],
  );
  const chatAvailableCount = useMemo(
    () => state.accounts.filter((account) => hasChatCapability(account.availability)).length,
    [state.accounts],
  );
  const imageAvailableCount = useMemo(
    () => state.accounts.filter((account) => hasImageCapability(account.availability)).length,
    [state.accounts],
  );
  const unavailableCount = useMemo(
    () => state.accounts.filter((account) => account.availability?.category === "unavailable").length,
    [state.accounts],
  );
  const testedCount = useMemo(
    () => state.accounts.filter((account) => Boolean(account.availability)).length,
    [state.accounts],
  );
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
  const failedAccounts = useMemo(() => {
    return [...(job?.failedAccounts || [])].reverse();
  }, [job?.failedAccounts]);

  useEffect(() => {
    setSelectedAccounts((current) => {
      const available = new Set(accountsWithKeys.map((account) => account.rowKey));
      const next = new Set([...current].filter((key) => available.has(key)));
      return next.size === current.size ? current : next;
    });
  }, [accountsWithKeys]);

  useEffect(() => {
    if (accountsWithKeys.length === 0) {
      if (activeTestAccountKey) setActiveTestAccountKey("");
      return;
    }
    if (!accountsWithKeys.some((account) => account.rowKey === activeTestAccountKey)) {
      setActiveTestAccountKey(accountsWithKeys[0].rowKey);
    }
  }, [accountsWithKeys, activeTestAccountKey]);

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

  async function retryRegistration() {
    if (retrying || isRunning) return;
    setRetrying(true);
    setError(null);
    try {
      const response = await apiJson<{ job: RegistrationJob }>("/api/register/retry", {
        method: "POST",
        body: "{}",
      });
      setState((current) => ({ ...current, job: response.job }));
    } catch (retryError) {
      setError(String(retryError));
    } finally {
      setRetrying(false);
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
    const exportKeys = selectedRows.map((account) => account.exportKey).filter((key): key is string => Boolean(key));
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

  async function testSelectedAccounts() {
    if (selectedAccounts.size === 0) return;
    const exportKeys = selectedRows.map((account) => account.exportKey).filter((key): key is string => Boolean(key));
    if (exportKeys.length === 0) return;
    setTestingAccounts(true);
    setError(null);
    try {
      const response = await apiJson<{ results: AccountTestResult[]; accounts: AccountRecord[] }>(
        "/api/accounts/test-batch",
        {
          method: "POST",
          body: JSON.stringify({ exportKeys, timeout: 180 }),
        },
      );
      setAccountTestResults(response.results || []);
      setState((current) => ({ ...current, accounts: response.accounts || current.accounts }));
    } catch (testError) {
      setError(String(testError));
    } finally {
      setTestingAccounts(false);
    }
  }

  async function deleteSelectedAccounts() {
    if (selectedAccounts.size === 0) return;
    const exportKeys = selectedRows.map((account) => account.exportKey).filter((key): key is string => Boolean(key));
    if (exportKeys.length === 0) return;
    const confirmed = window.confirm(`确认从数据库删除选中的 ${exportKeys.length} 个账号吗？凭证文件会保留。`);
    if (!confirmed) return;
    setError(null);
    try {
      const response = await apiJson<{ deleted: number; accounts: AccountRecord[] }>("/api/accounts/delete", {
        method: "POST",
        body: JSON.stringify({ exportKeys }),
      });
      setState((current) => ({ ...current, accounts: response.accounts || current.accounts }));
      setSelectedAccounts(new Set());
    } catch (deleteError) {
      setError(String(deleteError));
    }
  }

  async function refreshSelectedQuota() {
    if (selectedAccounts.size === 0) return;
    const selected = selectedRows.filter((account) => Boolean(account.id));
    if (selected.length === 0) return;
    setError(null);
    setRefreshingSelected(true);
    try {
      for (const account of selected) {
        await refreshQuota(account.id);
      }
      await refresh();
    } catch (refreshError) {
      setError(String(refreshError));
    } finally {
      setRefreshingSelected(false);
    }
  }

  async function sendChatMessage() {
    const prompt = chatPrompt.trim();
    if (!prompt || chatBusy) return;
    if (!activeTestAccount?.exportKey) {
      setError("请选择一个账号再测试 Chat");
      return;
    }
    const userMessage: ChatMessage = { id: `user-${Date.now()}`, role: "user", content: prompt };
    const assistantId = `assistant-${Date.now()}`;
    setChatMessages((current) => [...current, userMessage, { id: assistantId, role: "assistant", content: "请求中..." }]);
    setChatPrompt("");
    setChatBusy(true);
    setError(null);
    try {
      const response = await apiJson<{
        message?: { role: "assistant"; content: string };
        accounts?: AccountRecord[];
      }>("/api/accounts/chat-test", {
        method: "POST",
        body: JSON.stringify({
          exportKey: activeTestAccount.exportKey,
          model: chatModel,
          messages: [...chatMessages, userMessage].map((message) => ({
            role: message.role,
            content: message.content,
          })),
          timeout: 180,
        }),
      });
      const content = response.message?.content || "模型已响应，但没有返回文本内容";
      setChatMessages((current) =>
        current.map((message) => (message.id === assistantId ? { ...message, content } : message)),
      );
      if (response.accounts) {
        setState((current) => ({ ...current, accounts: response.accounts || current.accounts }));
      }
    } catch (chatError) {
      setChatMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? { ...message, content: "调用失败", error: String(chatError) }
            : message,
        ),
      );
    } finally {
      setChatBusy(false);
    }
  }

  async function generateImage() {
    const prompt = imagePrompt.trim();
    if (!prompt || imageBusy) return;
    if (!activeTestAccount?.exportKey) {
      setError("请选择一个账号再测试图片生成");
      return;
    }
    setImageBusy(true);
    setGeneratedImages([]);
    setError(null);
    try {
      const response = await apiJson<{ model?: string; data?: GeneratedImage[]; accounts?: AccountRecord[] }>("/api/accounts/image-test", {
        method: "POST",
        body: JSON.stringify({
          exportKey: activeTestAccount.exportKey,
          model: imageModel,
          prompt,
          n: imageCount,
          size: imageSize,
          timeout: 180,
        }),
      });
      if (response.model && response.model !== imageModel) {
        setImageModel(response.model);
      }
      setGeneratedImages(response.data || []);
      if (response.accounts) {
        setState((current) => ({ ...current, accounts: response.accounts || current.accounts }));
      }
    } catch (imageError) {
      setError(String(imageError));
    } finally {
      setImageBusy(false);
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
      const exportKeys = selectedRows.map((account) => account.exportKey).filter((key): key is string => Boolean(key));
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
              <textarea value={outlookData} disabled={isRunning} placeholder="email----password----clientId----refreshToken----auto/imap/graph 或 email|password|clientId|refreshToken|auto" spellCheck={false} onChange={(event) => setOutlookData(event.target.value)} />
              <small>{outlookAccountCount > 0 ? `已识别 ${outlookAccountCount} 个邮箱` : "未识别到有效账号"}</small>
            </label>
          )}
          {needsGoogleAccounts && (
            <label className="textarea-field">
              <span>{emailSource === "google" ? "Google 账号池" : "Gmail 邮箱池"}</span>
              <textarea value={googleData} disabled={isRunning} placeholder={emailSource === "google" ? "email----password----recoveryEmail(可选) 或 email|password|recoveryEmail" : "email----appPassword 或 email|appPassword"} spellCheck={false} onChange={(event) => setGoogleData(event.target.value)} />
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
            <span>超时 {job?.roundTimeoutSeconds ?? 180}s</span>
            <span>注册方式 {formatEmailSourceLabel(job)}</span>
          </div>
        </div>
      </div>
      {failedAccounts.length > 0 && (
        <div className="failed-accounts-block">
          <div className="sub-panel-head">
            <strong>失败账号</strong>
            <span>{failedAccounts.length} 条</span>
            <button
              className="secondary-btn"
              type="button"
              disabled={retrying || isRunning}
              onClick={() => void retryRegistration()}
              title="使用上次注册配置，重新注册一个账号"
            >
              {retrying ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
              重新注册一个
            </button>
          </div>
          <div className="accounts-table-wrap compact">
            <table className="accounts-table failed-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>邮箱</th>
                  <th>轮次</th>
                  <th>阶段</th>
                  <th>原因</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {failedAccounts.map((item) => (
                  <tr key={item.id}>
                    <td>{formatTime(item.time)}</td>
                    <td>{hideEmails ? maskEmail(item.email) : item.email}</td>
                    <td>#{item.round} / W{item.worker}</td>
                    <td><code>{item.stage || "-"}</code></td>
                    <td>{item.timedOut ? `超时：${item.reason}` : item.reason}</td>
                    <td>
                      <button
                        className="small-icon-btn"
                        type="button"
                        disabled={retrying || isRunning}
                        onClick={() => void retryRegistration()}
                        title="重新注册（使用上次配置注册一个新账号）"
                        aria-label="重新注册"
                      >
                        {retrying ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );

  const cliTestPanel = (
    <section className="module-panel">
      <div className="panel-head">
        <div>
          <h2>Grok CLI 测试</h2>
          <span>{selectedCount > 0 ? `已选择 ${selectedCount} 个账号` : "从账号池选择要验证的账号"}</span>
        </div>
        <div className="panel-actions">
          <button className="secondary-btn" type="button" disabled={selectedCount === 0 || refreshingSelected} onClick={() => void refreshSelectedQuota()}>
            {refreshingSelected ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
            刷新额度
          </button>
          <button className="primary-btn" type="button" disabled={selectedCount === 0 || testingAccounts} onClick={() => void testSelectedAccounts()}>
            {testingAccounts ? <Loader2 size={16} className="spin" /> : <Terminal size={16} />}
            测试 grok-4.5
          </button>
          <button className="export-btn" type="button" disabled={selectedCount === 0 || exporting !== null} onClick={() => void exportSelectedAccounts("json")}>
            <Download size={16} />
            导出 JSON
          </button>
          <button className="danger-btn" type="button" disabled={selectedCount === 0} onClick={() => void deleteSelectedAccounts()}>
            <Trash2 size={16} />
            删除
          </button>
        </div>
      </div>
      <div className="test-lanes">
        <div className="test-lane cli">
          <span>CLI 4.5 可用</span>
          <strong>{cli45Count}</strong>
        </div>
        <div className="test-lane chat">
          <span>Chat 4.20 可用</span>
          <strong>{chatAvailableCount}</strong>
        </div>
        <div className="test-lane image">
          <span>Imagine 生图</span>
          <strong>{imageAvailableCount}</strong>
        </div>
        <div className="test-lane missing">
          <span>不可用</span>
          <strong>{unavailableCount}</strong>
        </div>
      </div>
      {accountTestResults.length > 0 && (
        <div className="test-summary-row">
          <span>本次 CLI 4.5：{accountTestResults.filter((item) => hasCli45Capability(item)).length}</span>
          <span>本次 Chat 4.20：{accountTestResults.filter((item) => hasChatCapability(item)).length}</span>
          <span>本次生图：{accountTestResults.filter((item) => hasImageCapability(item)).length}</span>
        </div>
      )}
      <div className="accounts-table-wrap">
        <table className="accounts-table">
          <thead>
            <tr>
              <th className="select-col"><input type="checkbox" checked={allSelected} disabled={accountsWithKeys.length === 0} aria-label="选择全部账号" onChange={toggleAllAccounts} /></th>
              <th>账号</th>
              <th>注册时间</th>
              <th>CLI 4.5</th>
              <th>Chat 4.20</th>
              <th>生图</th>
              <th>套餐</th>
              <th>最近测试</th>
            </tr>
          </thead>
          <tbody>
            {accountsWithKeys.length === 0 ? (
              <tr><td colSpan={8}><div className="empty-row">暂无账号</div></td></tr>
            ) : (
              accountsWithKeys.map((account) => (
                <tr key={account.rowKey}>
                  <td className="select-col"><input type="checkbox" checked={selectedAccounts.has(account.rowKey)} aria-label={`选择 ${maskEmail(account.email)}`} onChange={() => toggleAccount(account.rowKey)} /></td>
                  <td><div className="email-cell"><strong>{hideEmails ? maskEmail(account.email) : account.email}</strong><span>{account.fileName}</span></div></td>
                  <td>{account.createdAtLabel || "-"}</td>
                  <td><CapabilityCell ok={hasCli45Capability(account.availability)} tested={Boolean(account.availability)} label="CLI 4.5" /></td>
                  <td><CapabilityCell ok={hasChatCapability(account.availability)} tested={Boolean(account.availability)} label="Chat" /></td>
                  <td><CapabilityCell ok={hasImageCapability(account.availability)} tested={Boolean(account.availability)} label="生图" /></td>
                  <td>{account.planType || "-"}</td>
                  <td>{account.availability?.testedAt ? formatTime(account.availability.testedAt) : "未测试"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );

  const chatTestPanel = (
    <section className="module-panel">
      <div className="panel-head">
        <div>
          <h2>Chat 对话测试</h2>
          <span>{activeTestAccount ? `当前账号：${hideEmails ? maskEmail(activeTestAccount.email) : activeTestAccount.email}` : "请选择账号"}</span>
        </div>
        <button className="secondary-btn" type="button" onClick={() => setChatMessages([])}>
          <Trash2 size={16} />
          清空
        </button>
      </div>
      <div className="test-console">
        <div className="test-config-grid two">
          <label>
            <span>测试账号</span>
            <select value={activeTestAccount?.rowKey || ""} onChange={(event) => setActiveTestAccountKey(event.target.value)}>
              {accountsWithKeys.length === 0 && <option value="">暂无账号</option>}
              {accountsWithKeys.map((account) => (
                <option key={account.rowKey} value={account.rowKey}>
                  {(hideEmails ? maskEmail(account.email) : account.email) || "unknown"} / {account.planType || "未识别套餐"}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>模型</span>
            <select value={chatModel} onChange={(event) => setChatModel(event.target.value)}>
              {CHAT_TEST_MODELS.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          </label>
        </div>
        <div className="chat-window">
          {chatMessages.length === 0 ? (
            <div className="empty-row">输入提示词后开始测试 Chat 4.20 模型</div>
          ) : (
            chatMessages.map((message) => (
              <div className={`chat-bubble ${message.role}`} key={message.id}>
                <span>{message.role === "user" ? "你" : "Grok"}</span>
                <p>{message.error || message.content}</p>
              </div>
            ))
          )}
        </div>
        <div className="sendbar">
          <textarea value={chatPrompt} placeholder="输入测试提示词" onChange={(event) => setChatPrompt(event.target.value)} />
          <button className="primary-btn" type="button" disabled={chatBusy || !chatPrompt.trim() || !activeTestAccount} onClick={() => void sendChatMessage()}>
            {chatBusy ? <Loader2 size={17} className="spin" /> : <Send size={17} />}
            发送
          </button>
        </div>
      </div>
    </section>
  );

  const imageTestPanel = (
    <section className="module-panel">
      <div className="panel-head">
        <div>
          <h2>图片生成测试</h2>
          <span>{activeTestAccount ? `当前账号：${hideEmails ? maskEmail(activeTestAccount.email) : activeTestAccount.email}` : "请选择账号"}</span>
        </div>
        <button className="secondary-btn" type="button" onClick={() => setGeneratedImages([])}>
          <Trash2 size={16} />
          清空
        </button>
      </div>
      <div className="test-console">
        <div className="test-config-grid four">
          <label>
            <span>测试账号</span>
            <select value={activeTestAccount?.rowKey || ""} onChange={(event) => setActiveTestAccountKey(event.target.value)}>
              {accountsWithKeys.length === 0 && <option value="">暂无账号</option>}
              {accountsWithKeys.map((account) => (
                <option key={account.rowKey} value={account.rowKey}>
                  {(hideEmails ? maskEmail(account.email) : account.email) || "unknown"} / {account.planType || "未识别套餐"}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>模型</span>
            <select value={imageModel} onChange={(event) => setImageModel(event.target.value)}>
              {IMAGE_TEST_MODELS.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          </label>
          <label>
            <span>尺寸</span>
            <select value={imageSize} onChange={(event) => setImageSize(event.target.value)}>
              {IMAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
          </label>
          <label>
            <span>数量</span>
            <select value={imageCount} onChange={(event) => setImageCount(Number(event.target.value) || 1)}>
              {[1, 2, 3, 4].map((count) => <option key={count} value={count}>{count} 张</option>)}
            </select>
          </label>
        </div>
        <div className="sendbar image-sendbar">
          <textarea value={imagePrompt} placeholder="描述你想生成的图片" onChange={(event) => setImagePrompt(event.target.value)} />
          <button className="primary-btn" type="button" disabled={imageBusy || !imagePrompt.trim() || !activeTestAccount} onClick={() => void generateImage()}>
            {imageBusy ? <Loader2 size={17} className="spin" /> : <Sparkles size={17} />}
            生成
          </button>
        </div>
        <div className="image-result-grid">
          {imageBusy && Array.from({ length: imageCount }).map((_, index) => (
            <div className="image-card loading" key={index}>
              <Loader2 size={24} className="spin" />
              <span>生成中</span>
            </div>
          ))}
          {!imageBusy && generatedImages.length === 0 && <div className="empty-row">还没有生成图片</div>}
          {!imageBusy && generatedImages.map((image, index) => (
            <div className="image-card" key={`${image.url || image.b64_json || index}`}>
              {imageSource(image) ? <img src={imageSource(image)} alt={`grok generated ${index + 1}`} /> : <span>无图片数据</span>}
              <div>
                <span>#{index + 1}</span>
                {imageSource(image) && (
                  <button className="small-icon-btn" type="button" title="复制图片地址" onClick={() => void copyText(imageSource(image))}>
                    <Copy size={14} />
                  </button>
                )}
              </div>
            </div>
          ))}
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
          <button className="secondary-btn account-test-btn" type="button" disabled={selectedCount === 0 || testingAccounts} onClick={() => void testSelectedAccounts()}>
            {testingAccounts ? <Loader2 size={16} className="spin" /> : <TestTube2 size={16} />}
            批量测试
          </button>
          <button className="danger-btn account-test-btn" type="button" disabled={selectedCount === 0} onClick={() => void deleteSelectedAccounts()}>
            <Trash2 size={16} />
            删除
          </button>
          <button className="small-icon-btn" type="button" onClick={() => void refresh()} aria-label="刷新账号列表" title="刷新账号列表">
            <RefreshCw size={16} />
          </button>
        </div>
      </div>
      {accountTestResults.length > 0 && (
        <div className="test-summary-row">
          <span>CLI 4.5：{accountTestResults.filter((item) => hasCli45Capability(item)).length}</span>
          <span>Chat 4.20：{accountTestResults.filter((item) => hasChatCapability(item)).length}</span>
          <span>Imagine 生图：{accountTestResults.filter((item) => hasImageCapability(item)).length}</span>
          <span>不可用：{accountTestResults.filter((item) => item.category === "unavailable").length}</span>
        </div>
      )}
      <div className="accounts-table-wrap">
        <table className="accounts-table">
          <thead>
            <tr>
              <th className="select-col"><input type="checkbox" checked={allSelected} disabled={accountsWithKeys.length === 0} aria-label="选择全部账号" onChange={toggleAllAccounts} /></th>
              <th>邮箱</th>
              <th>Refresh</th>
              <th>可用性</th>
              <th>用户</th>
              <th>套餐</th>
              <th>额度</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {accountsWithKeys.length === 0 ? (
              <tr><td colSpan={9}><div className="empty-row">暂无账号</div></td></tr>
            ) : (
              accountsWithKeys.map((account) => (
                <tr key={account.rowKey}>
                  <td className="select-col"><input type="checkbox" checked={selectedAccounts.has(account.rowKey)} aria-label={`选择 ${maskEmail(account.email)}`} onChange={() => toggleAccount(account.rowKey)} /></td>
                  <td><div className="email-cell"><strong>{hideEmails ? maskEmail(account.email) : account.email}</strong>{account.error && <span>{account.error}</span>}</div></td>
                  <td><span className={`token-badge ${account.hasRefreshToken ? "ok" : "missing"}`}>{account.hasRefreshToken && <ShieldCheck size={13} />}{account.hasRefreshToken ? "已获取" : "缺失"}</span></td>
                  <td><AvailabilityBadge availability={account.availability} /></td>
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

  const communityPanel = (
    <section className="community-panel">
      <div className="community-copy">
        <span className="eyebrow">PROJECT NOTICE</span>
        <h2>更新公告与交流群</h2>
        <p>
          这个项目会继续围绕账号池检测、Grok CLI 4.5、Chat 对话、图片生成和本地中转稳定性更新。
          如果项目帮到你，欢迎去 GitHub 帮忙点一个 Star，也可以加入 QQ 群反馈问题。
        </p>
        <div className="announcement-list">
          {ANNOUNCEMENT_ITEMS.map((item) => (
            <span key={item}><Star size={14} />{item}</span>
          ))}
        </div>
        <div className="community-actions">
          <a className="primary-btn" href={PROJECT_GITHUB_URL} target="_blank" rel="noreferrer">
            <Github size={17} />
            GitHub 点 Star
          </a>
          <button className="secondary-btn" type="button" onClick={() => void copyText(QQ_GROUP_NUMBER)}>
            <MessageCircle size={17} />
            复制群号
          </button>
        </div>
      </div>
      <div className="community-qr-card">
        <img src={COMMUNITY_QR_SRC} alt="QQ 交流群二维码" />
        <div>
          <strong>QQ 交流群</strong>
          <span>{QQ_GROUP_NUMBER}</span>
        </div>
      </div>
    </section>
  );

  const announcementDialog = announcementOpen ? (
    <div
      className="announcement-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeAnnouncement();
      }}
    >
      <section className="announcement-dialog" role="dialog" aria-modal="true" aria-labelledby="announcement-title">
        <button className="announcement-close" type="button" aria-label="关闭更新公告" onClick={closeAnnouncement}>
          <X size={18} />
        </button>
        <div className="announcement-dialog-head">
          <span><Megaphone size={15} />更新公告</span>
          <a href={PROJECT_GITHUB_URL} target="_blank" rel="noreferrer">
            <Github size={15} />
            {PROJECT_GITHUB_URL}
            <ExternalLink size={14} />
          </a>
        </div>
        <div className="announcement-dialog-body">
          <div>
            <h2 id="announcement-title">欢迎使用 MSDSJ Grok 控制台</h2>
            <p>
              如果这个项目帮到你，欢迎帮忙点一个 Star。后续每次版本更新都会在这里追加一条公告，方便你快速看到新功能和修复内容。
              <span style={{fontWeight:700,color:'red'}}>当前版本bug可能比较多，版本更新太多了token有一点不够，还在付费上班啦，有没有大哥投喂一下给点奶茶钱去买token，有bug请理解小弟已经尽可能腾出时间去优化去修复了，注册机目前是没啥问题哦！！！！！</span>
            </p>
            <div className="announcement-bullets">
              {ANNOUNCEMENT_ITEMS.map((item) => (
                <span key={item}><Star size={14} />{item}</span>
              ))}
            </div>
            <div className="community-actions">
              <a className="primary-btn" href={PROJECT_GITHUB_URL} target="_blank" rel="noreferrer">
                <Github size={17} />
                去 GitHub 点 Star
              </a>
              <button className="secondary-btn" type="button" onClick={() => void copyText(QQ_GROUP_NUMBER)}>
                <MessageCircle size={17} />
                复制群号
              </button>
            </div>
          </div>
          <div className="announcement-qr">
            <img src={COMMUNITY_QR_SRC} alt="QQ 交流群二维码" />
            <strong>加入 QQ 交流群</strong>
            <span>{QQ_GROUP_NUMBER}</span>
          </div>
        </div>
      </section>
    </div>
  ) : null;

  const overviewPanel = (
    <>
      <section className="overview-command">
        <div>
          <span className="eyebrow">ACCOUNT VAULT</span>
          <h2>{state.accounts.length}</h2>
          <p>本地账号资产</p>
        </div>
        <div className="command-ledger">
          <span>Refresh Token <strong>{refreshReadyCount}</strong></span>
          <span>已测试 <strong>{testedCount}</strong></span>
          <span>CLI 4.5 <strong>{cli45Count}</strong></span>
          <span>中转 <strong>{relayRunning ? "运行中" : "未启动"}</strong></span>
        </div>
        <div className="command-actions">
          <button className="primary-btn" type="button" onClick={() => navigateTo("accounts")}>
            <Users size={17} />
            账号列表
          </button>
          <button className="secondary-btn" type="button" onClick={() => navigateTo("relay")}>
            <Server size={17} />
            中转设置
          </button>
        </div>
      </section>

      <div className="stats-grid">
        <MetricCard icon={<Users size={20} />} label="账号总数" value={state.accounts.length} tone="teal" />
        <MetricCard icon={<KeyRound size={20} />} label="Refresh Token" value={refreshReadyCount} tone="indigo" />
        <MetricCard icon={<CheckCircle2 size={20} />} label="Chat 4.20" value={chatAvailableCount} tone="green" />
        <MetricCard icon={<ImageIcon size={20} />} label="Imagine 生图" value={imageAvailableCount} tone="pink" />
        <MetricCard icon={<Server size={20} />} label="本地中转" value={relayRunning ? "运行中" : "未启动"} text tone="amber" />
      </div>

      <section className="asset-panel">
        <div className="panel-head">
          <div>
            <h2>账号资产</h2>
            <span>{testedCount > 0 ? `已测试 ${testedCount} 个账号` : "等待批量测试"}</span>
          </div>
          <button className="icon-text-btn" type="button" onClick={() => navigateTo("accounts")}>
            <Users size={16} />
            管理账号
          </button>
        </div>
        <div className="asset-board">
          <div className="asset-row premium">
            <span>CLI 4.5</span>
            <div><i style={{ width: `${state.accounts.length ? Math.round((cli45Count / state.accounts.length) * 100) : 0}%` }} /></div>
            <strong>{cli45Count}</strong>
          </div>
          <div className="asset-row ok">
            <span>Chat 4.20</span>
            <div><i style={{ width: `${state.accounts.length ? Math.round((chatAvailableCount / state.accounts.length) * 100) : 0}%` }} /></div>
            <strong>{chatAvailableCount}</strong>
          </div>
          <div className="asset-row image">
            <span>Imagine 生图</span>
            <div><i style={{ width: `${state.accounts.length ? Math.round((imageAvailableCount / state.accounts.length) * 100) : 0}%` }} /></div>
            <strong>{imageAvailableCount}</strong>
          </div>
          <div className="asset-row missing">
            <span>不可用</span>
            <div><i style={{ width: `${state.accounts.length ? Math.round((unavailableCount / state.accounts.length) * 100) : 0}%` }} /></div>
            <strong>{unavailableCount}</strong>
          </div>
        </div>
      </section>

      {communityPanel}

      <div className="overview-grid">
        {relayPanel}
        {logsPanel}
      </div>
    </>
  );

  const activePanel = {
    overview: overviewPanel,
    register: registrationPanel,
    accounts: accountsPanel,
    cliTest: cliTestPanel,
    chatTest: chatTestPanel,
    imageTest: imageTestPanel,
    relay: relayPanel,
    logs: logsPanel,
  }[activeView];

  return (
    <>
    {announcementDialog}
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
          {appRoutes.map((route) => (
            <NavButton
              key={route.id}
              icon={route.icon}
              label={route.label}
              active={activeView === route.id}
              onClick={() => navigateTo(route.id)}
            />
          ))}
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
            <h1>{activeRoute.title}</h1>
            <p>{activeRoute.subtitle}</p>
          </div>
          <div className="topbar-actions">
            <button className="icon-text-btn" type="button" onClick={() => setAnnouncementOpen(true)}><Megaphone size={17} />公告</button>
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
    </>
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
  return entries.filter((entry) => {
    const parts = splitAccountFields(entry);
    return (parts.length === 4 || parts.length === 5) && Boolean(parts[0]?.trim()) && Boolean(parts[2]?.trim()) && Boolean(parts[3]?.trim());
  }).length;
}

function countGoogleAccounts(data: string): number {
  const raw = String(data || "").trim();
  if (!raw) return 0;
  return raw.split(/\r?\n/).filter((entry) => {
    const parts = splitAccountFields(entry);
    return parts.length >= 2 && Boolean(parts[0]?.trim()) && Boolean(parts[1]?.trim());
  }).length;
}

function splitAccountFields(entry: string): string[] {
  const value = String(entry || "").trim();
  if (!value) return [];
  if (/-{4,}/.test(value)) return value.split(/-{4,}/).map((part) => part.trim());
  if (value.includes("|")) return value.split("|").map((part) => part.trim());
  return [value];
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

function hasChatCapability(availability?: AccountAvailability): boolean {
  return Boolean(availability?.chatAvailable ?? availability?.baseAvailable);
}

function hasCli45Capability(availability?: AccountAvailability): boolean {
  return Boolean(
    availability?.cli45Available ||
      availability?.grok45Available ||
      availability?.category === "cli-4.5" ||
      availability?.category === "grok-4.5",
  );
}

function hasImageCapability(availability?: AccountAvailability): boolean {
  return Boolean(
    availability?.imageAvailable ||
      availability?.category === "image-only" ||
      availability?.category === "chat-image",
  );
}

function AvailabilityBadge({ availability }: { availability?: AccountAvailability }) {
  if (!availability) {
    return <span className="token-badge neutral">未测试</span>;
  }
  const badges: React.ReactNode[] = [];
  if (hasChatCapability(availability)) {
    badges.push(
      <span className="token-badge ok" title={availability.chatModel || availability.baseModel || "grok-4.20"} key="chat">
        Chat 4.20
      </span>,
    );
  }
  if (hasCli45Capability(availability)) {
    badges.push(
      <span className="token-badge premium" title={availability.cli45Model || availability.grok45Model || "grok-4.5"} key="cli45">
        CLI 4.5
      </span>,
    );
  }
  if (hasImageCapability(availability)) {
    badges.push(
      <span className="token-badge image" title={availability.imageModel || "grok-imagine-image-lite"} key="image">
        生图
      </span>,
    );
  }
  if (badges.length > 0) {
    return (
      <div className="availability-badges" title={availability.error || undefined}>
        {badges}
      </div>
    );
  }
  return (
    <span className="token-badge missing" title={availability.error || "不可用"}>
      不可用
    </span>
  );
}

function QuotaCell({ quota }: { quota: AccountRecord["quota"] }) {
  if (!quota) return <span style={{ color: "var(--muted)" }}>-</span>;
  const rows: string[] = [];
  if (quota.frequentLimit != null)
    rows.push(`高频 ${quota.frequentUsage ?? 0}/${quota.frequentLimit}`);
  if (quota.occasionalLimit != null)
    rows.push(`普通 ${quota.occasionalUsage ?? 0}/${quota.occasionalLimit}`);
  if (quota.weeklyTotal != null)
    rows.push(`周 ${quota.weeklyUsed ?? 0}/${quota.weeklyTotal}`);
  if (rows.length === 0) return <span style={{ color: "var(--muted)" }}>-</span>;
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

function capabilityLabel(value: ModelProbeRecord["capability"]): string {
  switch (value) {
    case "chat":
      return "Chat 4.20";
    case "image":
      return "Imagine 生图";
    case "image_edit":
      return "Imagine 改图";
    case "video":
      return "视频";
    default:
      return "未知";
  }
}

function extractChatContent(payload: {
  choices?: Array<{ message?: { content?: string | Array<{ text?: string }> } }>;
}): string {
  const content = payload.choices?.[0]?.message?.content;
  if (typeof content === "string") return content.trim();
  if (Array.isArray(content)) {
    return content.map((part) => part.text || "").join("").trim();
  }
  return "";
}

function imageSource(image: GeneratedImage): string {
  if (image.url) return image.url;
  if (image.b64_json) return `data:image/png;base64,${image.b64_json}`;
  return "";
}

async function copyText(value: string) {
  if (!value || value === "-") return;
  await navigator.clipboard.writeText(value);
}

function CapabilityCell({ ok, tested, label }: { ok: boolean; tested: boolean; label: string }) {
  return <span className={`token-badge ${ok ? "ok" : "neutral"}`}>{ok ? label : tested ? "不可用" : "待测"}</span>;
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
  tone?: "teal" | "indigo" | "green" | "pink" | "amber";
}) {
  return (
    <div className={`metric-card ${tone}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong className={text ? "metric-text" : ""}>{value}</strong>
    </div>
  );
}
