import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Play, RefreshCw, RotateCw, Save, Square, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import {
  countOutlookAccounts,
  countGoogleAccounts,
  clearRegistrationProxyPool,
  getRegistrationProxyPool,
  getCurrentJob,
  getOutlookMailboxPool,
  retryRegistration,
  saveOutlookMailboxPool,
  saveRegistrationProxyPool,
  startRegistration,
  stopRegistration,
  syncAccountsToPool,
  type EmailSource,
  type JobStatus,
} from "@/features/register-task/register-task-api";

const EMAIL_SOURCES: EmailSource[] = ["duckmail", "cloud_mail", "outlook", "google"];

const EMAIL_SOURCE_I18N_KEY: Record<EmailSource, string> = {
  duckmail: "registerTask.emailSourceDuckmail",
  cloud_mail: "registerTask.emailSourceCloudMail",
  outlook: "registerTask.emailSourceOutlook",
  google: "registerTask.emailSourceGoogle",
};

const STATUS_I18N_KEY: Record<JobStatus, string> = {
  running: "registerTask.statusRunning",
  stopping: "registerTask.statusStopping",
  completed: "registerTask.statusCompleted",
  completed_with_errors: "registerTask.statusCompletedWithErrors",
  stopped: "registerTask.statusStopped",
};

function statusTone(status?: JobStatus): "default" | "secondary" | "destructive" | "outline" {
  if (status === "running") return "default";
  if (status === "completed_with_errors") return "destructive";
  if (status === "stopping") return "secondary";
  return "outline";
}

export function RegisterTaskPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [total, setTotal] = useState(1);
  const [concurrency, setConcurrency] = useState(1);
  const [emailSource, setEmailSource] = useState<EmailSource>("duckmail");
  const [oauthExchange, setOauthExchange] = useState(true);
  const [autoImportSub2Api, setAutoImportSub2Api] = useState(false);
  const [minimizeBrowsers, setMinimizeBrowsers] = useState(true);
  // Undefined keeps the backend's automatic discovery behavior: use the
  // default file or saved nodes when present, otherwise stay on direct mode.
  const [proxyPoolEnabled, setProxyPoolEnabled] = useState<boolean | undefined>(undefined);
  const [proxyFile, setProxyFile] = useState("");
  const [proxyDialogOpen, setProxyDialogOpen] = useState(false);
  const [proxyData, setProxyData] = useState("");
  const [proxyReplace, setProxyReplace] = useState(false);
  const [outlookData, setOutlookData] = useState("");
  const [googleData, setGoogleData] = useState("");
  const [cloudMailApiBase, setCloudMailApiBase] = useState("");
  const [cloudMailDomains, setCloudMailDomains] = useState("");
  const [cloudMailAuthMode, setCloudMailAuthMode] = useState<"public" | "login">("public");
  const [cloudMailPublicToken, setCloudMailPublicToken] = useState("");
  const [cloudMailLoginEmail, setCloudMailLoginEmail] = useState("");
  const [cloudMailLoginPassword, setCloudMailLoginPassword] = useState("");
  const loadedOutlookPool = useRef(false);

  const jobQuery = useQuery({
    queryKey: ["register-job"],
    queryFn: getCurrentJob,
    refetchInterval: 3000,
  });

  const outlookPoolQuery = useQuery({
    queryKey: ["outlook-mailbox-pool"],
    queryFn: getOutlookMailboxPool,
    enabled: emailSource === "outlook",
    staleTime: Infinity,
  });

  const proxyPoolQuery = useQuery({
    queryKey: ["registration-proxy-pool"],
    queryFn: getRegistrationProxyPool,
    staleTime: 10_000,
  });

  useEffect(() => {
    const savedData = outlookPoolQuery.data?.data;
    if (
      emailSource === "outlook"
      && !loadedOutlookPool.current
      && savedData
      && !outlookData.trim()
    ) {
      setOutlookData(savedData);
      loadedOutlookPool.current = true;
    }
  }, [emailSource, outlookData, outlookPoolQuery.data?.data]);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["register-job"] });

  const startMutation = useMutation({
    mutationFn: () =>
      startRegistration({
        total,
        concurrency,
        oauthExchange,
        autoImportSub2Api,
        minimizeBrowsers,
        emailSource,
        outlookData: emailSource === "outlook" ? outlookData : "",
        googleData: emailSource === "google" ? googleData : "",
        cloudMailApiBase: emailSource === "cloud_mail" ? cloudMailApiBase : "",
        cloudMailDomains: emailSource === "cloud_mail" ? cloudMailDomains : "",
        cloudMailPublicToken: emailSource === "cloud_mail" && cloudMailAuthMode === "public" ? cloudMailPublicToken : "",
        cloudMailLoginEmail: emailSource === "cloud_mail" && cloudMailAuthMode === "login" ? cloudMailLoginEmail : "",
        cloudMailLoginPassword: emailSource === "cloud_mail" && cloudMailAuthMode === "login" ? cloudMailLoginPassword : "",
        proxyPoolEnabled,
        proxyFile,
      }),
    onSuccess: () => { toast.success(t("registerTask.started")); invalidate(); },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("registerTask.startFailed")),
  });

  const stopMutation = useMutation({
    mutationFn: stopRegistration,
    onSuccess: () => { toast.success(t("registerTask.stopped")); invalidate(); },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const retryMutation = useMutation({
    mutationFn: retryRegistration,
    onSuccess: () => { toast.success(t("registerTask.started")); invalidate(); },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("registerTask.startFailed")),
  });

  const syncMutation = useMutation({
    mutationFn: syncAccountsToPool,
    onSuccess: (result) => toast.success(t("registerTask.syncCompleted", { count: result.requested })),
    onError: (error) => toast.error(error instanceof Error ? error.message : t("registerTask.syncFailed")),
  });

  const saveOutlookPoolMutation = useMutation({
    mutationFn: () => saveOutlookMailboxPool(outlookData),
    onSuccess: (result) => {
      queryClient.setQueryData(["outlook-mailbox-pool"], {
        data: outlookData.trim() ? `${outlookData.trim()}\n` : "",
        count: result.count,
        invalid: 0,
        accounts: result.accounts,
      });
      loadedOutlookPool.current = true;
      toast.success(t("registerTask.outlookPoolSaved", { count: result.count }));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("registerTask.outlookPoolSaveFailed")),
  });

  const saveProxyPoolMutation = useMutation({
    mutationFn: () => saveRegistrationProxyPool(proxyData, proxyReplace),
    onSuccess: (result) => {
      queryClient.setQueryData(["registration-proxy-pool"], result);
      setProxyData("");
      setProxyDialogOpen(false);
      toast.success(t("registerTask.proxyPoolSaved", { count: result.count, added: result.added }));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("registerTask.proxyPoolSaveFailed")),
  });

  const clearProxyPoolMutation = useMutation({
    mutationFn: clearRegistrationProxyPool,
    onSuccess: (result) => {
      queryClient.setQueryData(["registration-proxy-pool"], result);
      toast.success(t("registerTask.proxyPoolCleared", { count: result.removed }));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("registerTask.proxyPoolSaveFailed")),
  });

  const loadSavedOutlookPool = async () => {
    try {
      const result = await outlookPoolQuery.refetch();
      if (!result.data) {
        throw new Error(t("registerTask.outlookPoolLoadFailed"));
      }
      setOutlookData(result.data.data);
      loadedOutlookPool.current = true;
      toast.success(t("registerTask.outlookPoolLoaded", { count: result.data.count }));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("registerTask.outlookPoolLoadFailed"));
    }
  };

  const openProxyDialog = () => {
    setProxyData("");
    setProxyReplace(false);
    setProxyDialogOpen(true);
  };

  const job = jobQuery.data ?? null;
  const running = job?.status === "running" || job?.status === "stopping";
  const events = job?.events?.slice().reverse().slice(0, 50) ?? [];
  const outlookAccountCount = countOutlookAccounts(outlookData);
  const googleAccountCount = countGoogleAccounts(googleData);
  const cloudMailReady = Boolean(
    cloudMailApiBase.trim()
    && cloudMailDomains.trim()
    && (
      (cloudMailAuthMode === "public" && cloudMailPublicToken.trim())
      || (cloudMailAuthMode === "login" && cloudMailLoginEmail.trim() && cloudMailLoginPassword.trim())
    ),
  );
  const startDisabled =
    running
    || startMutation.isPending
    || (emailSource === "outlook" && outlookAccountCount === 0)
    || (emailSource === "google" && googleAccountCount === 0)
    || (emailSource === "cloud_mail" && !cloudMailReady);

  return (
    <div className="space-y-5">
      <header className="flex min-h-8 items-center">
        <h1 className="text-xl font-medium">{t("registerTask.title")}</h1>
        <p className="sr-only">{t("registerTask.description")}</p>
      </header>

      <div className="grid gap-4 lg:grid-cols-2">
        <DashboardPanel id="register-form" title={t("registerTask.title")}>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="reg-total">{t("registerTask.totalLabel")}</Label>
                <Input
                  id="reg-total"
                  type="number"
                  min={1}
                  max={10000}
                  value={total}
                  disabled={running}
                  onChange={(event) => setTotal(Math.max(1, Number(event.target.value) || 1))}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reg-concurrency">{t("registerTask.concurrencyLabel")}</Label>
                <Input
                  id="reg-concurrency"
                  type="number"
                  min={1}
                  max={20}
                  value={concurrency}
                  disabled={running}
                  onChange={(event) => setConcurrency(Math.max(1, Number(event.target.value) || 1))}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>{t("registerTask.emailSourceLabel")}</Label>
              <Select value={emailSource} onValueChange={(value) => setEmailSource(value as EmailSource)} disabled={running}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {EMAIL_SOURCES.map((source) => (
                    <SelectItem key={source} value={source}>{t(EMAIL_SOURCE_I18N_KEY[source])}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {emailSource === "outlook" ? (
              <div className="space-y-1.5">
                <Label htmlFor="reg-outlook-data">{t("registerTask.outlookPoolLabel")}</Label>
                <Textarea
                  id="reg-outlook-data"
                  value={outlookData}
                  disabled={running}
                  spellCheck={false}
                  placeholder={t("registerTask.outlookPoolPlaceholder")}
                  onChange={(event) => setOutlookData(event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {outlookAccountCount > 0
                    ? t("registerTask.poolRecognizedCount", { count: outlookAccountCount })
                    : t("registerTask.poolNoneRecognized")}
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    type="button"
                    variant="secondary"
                    disabled={running || outlookPoolQuery.isFetching}
                    onClick={() => void loadSavedOutlookPool()}
                  >
                    <FolderOpen />{t("registerTask.loadOutlookPool")}
                  </Button>
                  <Button
                    size="sm"
                    type="button"
                    variant="secondary"
                    disabled={running || outlookAccountCount === 0 || saveOutlookPoolMutation.isPending}
                    onClick={() => saveOutlookPoolMutation.mutate()}
                  >
                    <Save />{t("registerTask.saveOutlookPool")}
                  </Button>
                </div>
              </div>
            ) : null}
            {emailSource === "google" ? (
              <div className="space-y-1.5">
                <Label htmlFor="reg-google-data">{t("registerTask.googlePoolLabel")}</Label>
                <Textarea
                  id="reg-google-data"
                  value={googleData}
                  disabled={running}
                  spellCheck={false}
                  placeholder={t("registerTask.googlePoolPlaceholder")}
                  onChange={(event) => setGoogleData(event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {googleAccountCount > 0
                    ? t("registerTask.poolRecognizedCount", { count: googleAccountCount })
                    : t("registerTask.poolNoneRecognized")}
                </p>
              </div>
            ) : null}
            {emailSource === "cloud_mail" ? (
              <div className="space-y-3 border-y py-3">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="reg-cloud-mail-api">{t("registerTask.cloudMailApiBaseLabel")}</Label>
                    <Input
                      id="reg-cloud-mail-api"
                      value={cloudMailApiBase}
                      disabled={running}
                      placeholder={t("registerTask.cloudMailApiBasePlaceholder")}
                      onChange={(event) => setCloudMailApiBase(event.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="reg-cloud-mail-domains">{t("registerTask.cloudMailDomainsLabel")}</Label>
                    <Input
                      id="reg-cloud-mail-domains"
                      value={cloudMailDomains}
                      disabled={running}
                      placeholder={t("registerTask.cloudMailDomainsPlaceholder")}
                      onChange={(event) => setCloudMailDomains(event.target.value)}
                    />
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>{t("registerTask.cloudMailAuthModeLabel")}</Label>
                  <Tabs value={cloudMailAuthMode} onValueChange={(value) => setCloudMailAuthMode(value as "public" | "login")}>
                    <TabsList>
                      <TabsTrigger value="public" disabled={running}>{t("registerTask.cloudMailAuthPublic")}</TabsTrigger>
                      <TabsTrigger value="login" disabled={running}>{t("registerTask.cloudMailAuthLogin")}</TabsTrigger>
                    </TabsList>
                  </Tabs>
                </div>
                {cloudMailAuthMode === "public" ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="reg-cloud-mail-token">{t("registerTask.cloudMailPublicTokenLabel")}</Label>
                    <Input
                      id="reg-cloud-mail-token"
                      type="password"
                      autoComplete="off"
                      value={cloudMailPublicToken}
                      disabled={running}
                      onChange={(event) => setCloudMailPublicToken(event.target.value)}
                    />
                  </div>
                ) : (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="reg-cloud-mail-email">{t("registerTask.cloudMailLoginEmailLabel")}</Label>
                      <Input
                        id="reg-cloud-mail-email"
                        type="email"
                        autoComplete="username"
                        value={cloudMailLoginEmail}
                        disabled={running}
                        onChange={(event) => setCloudMailLoginEmail(event.target.value)}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="reg-cloud-mail-password">{t("registerTask.cloudMailLoginPasswordLabel")}</Label>
                      <Input
                        id="reg-cloud-mail-password"
                        type="password"
                        autoComplete="current-password"
                        value={cloudMailLoginPassword}
                        disabled={running}
                        onChange={(event) => setCloudMailLoginPassword(event.target.value)}
                      />
                    </div>
                  </div>
                )}
                <p className="text-xs text-muted-foreground">{t("registerTask.cloudMailHelp")}</p>
              </div>
            ) : null}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="reg-proxy-pool">{t("registerTask.proxyPoolLabel")}</Label>
                <Switch
                  id="reg-proxy-pool"
                  checked={proxyPoolEnabled !== false}
                  disabled={running}
                  onCheckedChange={(checked) => setProxyPoolEnabled(checked)}
                />
              </div>
              <Input
                id="reg-proxy-file"
                value={proxyFile}
                disabled={running || proxyPoolEnabled === false}
                placeholder={t("registerTask.proxyFilePlaceholder")}
                onChange={(event) => setProxyFile(event.target.value)}
              />
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">
                  {t("registerTask.proxyPoolHelp")}
                  {proxyPoolQuery.data ? ` ${t("registerTask.proxySavedCount", { count: proxyPoolQuery.data.count })}` : ""}
                </p>
                <Button type="button" size="sm" variant="secondary" disabled={running} onClick={openProxyDialog}>
                  <Upload />{t("registerTask.manageProxyPool")}
                </Button>
              </div>
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="reg-oauth">{t("registerTask.oauthExchangeLabel")}</Label>
              <Switch
                id="reg-oauth"
                checked={oauthExchange}
                disabled={running}
                onCheckedChange={(checked) => {
                  setOauthExchange(checked);
                  if (!checked) setAutoImportSub2Api(false);
                }}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="reg-sub2api">{t("registerTask.autoImportSub2ApiLabel")}</Label>
              <Switch
                id="reg-sub2api"
                checked={autoImportSub2Api}
                disabled={running || !oauthExchange}
                onCheckedChange={setAutoImportSub2Api}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label htmlFor="reg-minimize">{t("registerTask.minimizeBrowsersLabel")}</Label>
              <Switch id="reg-minimize" checked={minimizeBrowsers} disabled={running} onCheckedChange={setMinimizeBrowsers} />
            </div>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button size="sm" disabled={startDisabled} onClick={() => startMutation.mutate()}>
                <Play />{t("registerTask.start")}
              </Button>
              <Button size="sm" variant="secondary" disabled={!running || stopMutation.isPending} onClick={() => stopMutation.mutate()}>
                <Square />{t("registerTask.stop")}
              </Button>
              <Button size="sm" variant="secondary" disabled={running || retryMutation.isPending} onClick={() => retryMutation.mutate()}>
                <RotateCw />{t("registerTask.retry")}
              </Button>
              <Button size="sm" variant="secondary" disabled={syncMutation.isPending} onClick={() => syncMutation.mutate()}>
                <RefreshCw />{t("registerTask.syncNow")}
              </Button>
            </div>
          </div>
        </DashboardPanel>

        <DashboardPanel
          id="register-status"
          title={t("registerTask.title")}
          actions={job ? <Badge variant={statusTone(job.status)}>{t(STATUS_I18N_KEY[job.status])}</Badge> : undefined}
        >
          {job ? (
            <div className="space-y-3 text-sm">
              <div className="text-muted-foreground">{t("registerTask.progress", { completed: job.completed, total: job.total })}</div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
                <span>{t("registerTask.succeeded", { count: job.registered })}</span>
                <span>{t("registerTask.failed", { count: job.failed })}</span>
                {job.autoImportSub2Api ? (
                  <span>{t("registerTask.sub2ApiProgress", { succeeded: job.sub2ApiImported, failed: job.sub2ApiImportFailed })}</span>
                ) : null}
              </div>
              {job.proxyPoolEnabled ? (
                <div className="text-xs text-muted-foreground">
                  {t("registerTask.proxyPoolProgress", {
                    used: job.proxyPoolUsed,
                    total: job.proxyPoolTotal,
                    remaining: job.proxyPoolRemaining,
                  })}
                </div>
              ) : null}
              {job.workers?.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>{t("accounts.status")}</TableHead>
                      <TableHead>{t("registerTask.proxyColumn")}</TableHead>
                      <TableHead>{t("accounts.account")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {job.workers.map((worker) => (
                      <TableRow key={worker.worker}>
                        <TableCell>{worker.worker}</TableCell>
                        <TableCell className="whitespace-nowrap text-xs">{worker.stage || worker.status}</TableCell>
                        <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{worker.proxy || "直连"}</TableCell>
                        <TableCell className="truncate text-xs text-muted-foreground">{worker.email || worker.message}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">{t("registerTask.statusIdle")}</p>
          )}
        </DashboardPanel>
      </div>

      <DashboardPanel id="register-events" title={t("registerTask.recentEvents")}>
        {events.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("registerTask.noEvents")}</p>
        ) : (
          <ul className="space-y-1.5 text-xs">
            {events.map((event, index) => (
              <li key={`${event.at}-${index}`} className={event.level === "error" ? "text-destructive" : event.level === "warning" ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground"}>
                {event.message}
              </li>
            ))}
          </ul>
        )}
      </DashboardPanel>

      <Dialog open={proxyDialogOpen} onOpenChange={setProxyDialogOpen}>
        <DialogContent className="max-h-[calc(100svh-2rem)] overflow-y-auto sm:max-w-[620px]">
          <DialogHeader>
            <DialogTitle>{t("registerTask.proxyDialogTitle")}</DialogTitle>
            <DialogDescription>{t("registerTask.proxyDialogDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <Textarea
              value={proxyData}
              onChange={(event) => setProxyData(event.target.value)}
              disabled={saveProxyPoolMutation.isPending || clearProxyPoolMutation.isPending}
              spellCheck={false}
              placeholder={t("registerTask.proxyDialogPlaceholder")}
              className="min-h-44 font-mono text-xs"
            />
            <div className="flex items-center justify-between gap-3">
              <div className="text-xs text-muted-foreground">
                {proxyPoolQuery.data
                  ? t("registerTask.proxySavedList", { count: proxyPoolQuery.data.count })
                  : t("registerTask.proxySavedLoading")}
                {proxyPoolQuery.data?.items.length ? `：${proxyPoolQuery.data.items.slice(0, 12).join("、")}` : ""}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Label htmlFor="reg-proxy-replace" className="text-xs">{t("registerTask.proxyReplaceLabel")}</Label>
                <Switch id="reg-proxy-replace" checked={proxyReplace} onCheckedChange={setProxyReplace} disabled={saveProxyPoolMutation.isPending || clearProxyPoolMutation.isPending} />
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" size="sm" variant="secondary" onClick={() => clearProxyPoolMutation.mutate()} disabled={proxyPoolQuery.data?.count === 0 || saveProxyPoolMutation.isPending || clearProxyPoolMutation.isPending}>
              <Trash2 />{t("registerTask.clearProxyPool")}
            </Button>
            <Button type="button" size="sm" variant="secondary" onClick={() => setProxyDialogOpen(false)} disabled={saveProxyPoolMutation.isPending || clearProxyPoolMutation.isPending}>{t("common.cancel")}</Button>
            <Button type="button" size="sm" onClick={() => saveProxyPoolMutation.mutate()} disabled={!proxyData.trim() || saveProxyPoolMutation.isPending || clearProxyPoolMutation.isPending}>
              <Save />{t("registerTask.saveProxyPool")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
