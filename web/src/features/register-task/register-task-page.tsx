import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw, RotateCw, Square } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import {
  countOutlookAccounts,
  getCurrentJob,
  retryRegistration,
  startRegistration,
  stopRegistration,
  syncAccountsToPool,
  type EmailSource,
  type JobStatus,
} from "@/features/register-task/register-task-api";

const EMAIL_SOURCES: EmailSource[] = ["duckmail", "outlook"];

const EMAIL_SOURCE_I18N_KEY: Record<EmailSource, string> = {
  duckmail: "registerTask.emailSourceDuckmail",
  outlook: "registerTask.emailSourceOutlook",
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
  const [minimizeBrowsers, setMinimizeBrowsers] = useState(true);
  const [outlookData, setOutlookData] = useState("");

  const jobQuery = useQuery({
    queryKey: ["register-job"],
    queryFn: getCurrentJob,
    refetchInterval: 3000,
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["register-job"] });

  const startMutation = useMutation({
    mutationFn: () =>
      startRegistration({
        total,
        concurrency,
        oauthExchange,
        minimizeBrowsers,
        emailSource,
        outlookData: emailSource === "outlook" ? outlookData : "",
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

  const job = jobQuery.data ?? null;
  const running = job?.status === "running" || job?.status === "stopping";
  const events = job?.events?.slice().reverse().slice(0, 50) ?? [];
  const outlookAccountCount = countOutlookAccounts(outlookData);
  const startDisabled =
    running
    || startMutation.isPending
    || (emailSource === "outlook" && outlookAccountCount === 0);

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
              </div>
            ) : null}
            <div className="flex items-center justify-between">
              <Label htmlFor="reg-oauth">{t("registerTask.oauthExchangeLabel")}</Label>
              <Switch id="reg-oauth" checked={oauthExchange} disabled={running} onCheckedChange={setOauthExchange} />
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
              <div className="flex gap-4 text-muted-foreground">
                <span>{t("registerTask.succeeded", { count: job.registered })}</span>
                <span>{t("registerTask.failed", { count: job.failed })}</span>
              </div>
              {job.workers?.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>{t("accounts.status")}</TableHead>
                      <TableHead>{t("accounts.account")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {job.workers.map((worker) => (
                      <TableRow key={worker.worker}>
                        <TableCell>{worker.worker}</TableCell>
                        <TableCell className="whitespace-nowrap text-xs">{worker.stage || worker.status}</TableCell>
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
    </div>
  );
}
