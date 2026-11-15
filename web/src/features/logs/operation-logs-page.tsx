import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import { listJobLogs, listOperationLogs, listSystemLogs } from "@/features/logs/logs-api";
import { PageHeader } from "@/shared/components/page-header";
import { formatDateTime } from "@/shared/lib/format";

type TabKey = "operations" | "jobs" | "system";

function formatTime(value: number, locale: string): string {
  if (!value) return "-";
  return formatDateTime(new Date(value).toISOString(), locale);
}

function resultTone(result: string): "default" | "destructive" | "outline" {
  if (result === "success") return "default";
  if (result.startsWith("failed")) return "destructive";
  return "outline";
}

export function OperationLogsPage() {
  const { t, i18n } = useTranslation();
  const [tab, setTab] = useState<TabKey>("operations");
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [result, setResult] = useState("all");
  const [jobId, setJobId] = useState("");
  const [jobQueryId, setJobQueryId] = useState("");
  const [level, setLevel] = useState("all");
  const [source, setSource] = useState("");

  const operationsQuery = useQuery({
    queryKey: ["operation-logs", actor, action, result],
    queryFn: () =>
      listOperationLogs({
        actor,
        action,
        result: result === "all" ? "" : result,
      }),
    enabled: tab === "operations",
  });
  const jobLogsQuery = useQuery({
    queryKey: ["job-logs", jobQueryId],
    queryFn: () => listJobLogs(jobQueryId),
    enabled: tab === "jobs" && Boolean(jobQueryId.trim()),
  });
  const systemLogsQuery = useQuery({
    queryKey: ["system-logs", level, source],
    queryFn: () =>
      listSystemLogs({
        level: level === "all" ? "" : level,
        source,
      }),
    enabled: tab === "system",
  });

  const refresh = () => {
    if (tab === "operations") void operationsQuery.refetch();
    if (tab === "jobs") void jobLogsQuery.refetch();
    if (tab === "system") void systemLogsQuery.refetch();
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("operationLogs.title")}
        description={t("operationLogs.description")}
        actions={(
          <Button size="sm" variant="secondary" onClick={refresh}>
            <RefreshCw />{t("operationLogs.refresh")}
          </Button>
        )}
      />
      <DashboardPanel
        id="operation-logs"
        title={tab === "operations" ? t("operationLogs.tabOperations") : tab === "jobs" ? t("operationLogs.tabJobs") : t("operationLogs.tabSystem")}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Tabs value={tab} onValueChange={(value) => setTab(value as TabKey)}>
            <TabsList>
              <TabsTrigger value="operations">{t("operationLogs.tabOperations")}</TabsTrigger>
              <TabsTrigger value="jobs">{t("operationLogs.tabJobs")}</TabsTrigger>
              <TabsTrigger value="system">{t("operationLogs.tabSystem")}</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>

        {tab === "operations" ? (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap gap-2">
              <Input className="w-44" value={actor} placeholder={t("operationLogs.actor")} onChange={(event) => setActor(event.target.value)} />
              <Input className="w-52" value={action} placeholder={t("operationLogs.action")} onChange={(event) => setAction(event.target.value)} />
              <Select value={result} onValueChange={setResult}>
                <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t("operationLogs.result")}</SelectItem>
                  <SelectItem value="success">{t("operationLogs.success")}</SelectItem>
                  <SelectItem value="failed">{t("operationLogs.failed")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {operationsQuery.isLoading ? <div className="flex justify-center py-10"><Spinner className="size-5" /></div> : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("operationLogs.time")}</TableHead>
                    <TableHead>{t("operationLogs.actor")}</TableHead>
                    <TableHead>{t("operationLogs.action")}</TableHead>
                    <TableHead>{t("operationLogs.target")}</TableHead>
                    <TableHead>{t("operationLogs.result")}</TableHead>
                    <TableHead>{t("operationLogs.detail")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(operationsQuery.data?.items ?? []).map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="whitespace-nowrap text-xs">{formatTime(item.createdAt, i18n.language)}</TableCell>
                      <TableCell className="text-xs">{item.actor}</TableCell>
                      <TableCell className="text-xs">{item.action}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{item.targetId || item.targetType || "-"}</TableCell>
                      <TableCell><Badge variant={resultTone(item.result)}>{item.result}</Badge></TableCell>
                      <TableCell className="max-w-80 truncate text-xs text-muted-foreground">{JSON.stringify(item.detail)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {(operationsQuery.data?.items ?? []).length === 0 && !operationsQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">{t("operationLogs.noLogs")}</p>
            ) : null}
          </div>
        ) : null}

        {tab === "jobs" ? (
          <div className="mt-4 space-y-3">
            <div className="flex gap-2">
              <Input
                className="w-96"
                value={jobId}
                placeholder={t("operationLogs.jobIdPlaceholder")}
                onChange={(event) => setJobId(event.target.value)}
              />
              <Button size="sm" onClick={() => setJobQueryId(jobId.trim())}>{t("operationLogs.refresh")}</Button>
            </div>
            {jobLogsQuery.isLoading ? <div className="flex justify-center py-10"><Spinner className="size-5" /></div> : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("operationLogs.time")}</TableHead>
                    <TableHead>{t("operationLogs.level")}</TableHead>
                    <TableHead>Worker</TableHead>
                    <TableHead>{t("operationLogs.message")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(jobLogsQuery.data?.items ?? []).map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="whitespace-nowrap text-xs">{formatTime(item.createdAt, i18n.language)}</TableCell>
                      <TableCell><Badge variant={item.level === "error" ? "destructive" : item.level === "warning" ? "secondary" : "outline"}>{item.level}</Badge></TableCell>
                      <TableCell className="text-xs">{item.worker || "-"}</TableCell>
                      <TableCell className="text-xs">{item.message}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {jobQueryId && (jobLogsQuery.data?.items ?? []).length === 0 && !jobLogsQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">{t("operationLogs.noLogs")}</p>
            ) : null}
          </div>
        ) : null}

        {tab === "system" ? (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap gap-2">
              <Select value={level} onValueChange={setLevel}>
                <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t("operationLogs.level")}</SelectItem>
                  <SelectItem value="info">info</SelectItem>
                  <SelectItem value="warning">warning</SelectItem>
                  <SelectItem value="error">error</SelectItem>
                </SelectContent>
              </Select>
              <Input className="w-52" value={source} placeholder={t("operationLogs.source")} onChange={(event) => setSource(event.target.value)} />
            </div>
            {systemLogsQuery.isLoading ? <div className="flex justify-center py-10"><Spinner className="size-5" /></div> : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("operationLogs.time")}</TableHead>
                    <TableHead>{t("operationLogs.level")}</TableHead>
                    <TableHead>{t("operationLogs.source")}</TableHead>
                    <TableHead>{t("operationLogs.message")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(systemLogsQuery.data?.items ?? []).map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="whitespace-nowrap text-xs">{formatTime(item.createdAt, i18n.language)}</TableCell>
                      <TableCell><Badge variant={item.level === "error" ? "destructive" : item.level === "warning" ? "secondary" : "outline"}>{item.level}</Badge></TableCell>
                      <TableCell className="text-xs">{item.source || "-"}</TableCell>
                      <TableCell className="text-xs">{item.message}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {(systemLogsQuery.data?.items ?? []).length === 0 && !systemLogsQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">{t("operationLogs.noLogs")}</p>
            ) : null}
          </div>
        ) : null}
      </DashboardPanel>
    </div>
  );
}
