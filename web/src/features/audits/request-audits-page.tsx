import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { listGatewayRequests, type GatewayRequestDTO } from "@/shared/api/gateway";
import { DataTableFilters } from "@/shared/components/data-table-filters";
import { DataTableShell } from "@/shared/components/data-table-shell";
import { EmptyState, ErrorState, TableLoadingRow } from "@/shared/components/data-state";
import { PageHeader } from "@/shared/components/page-header";
import { Pagination } from "@/shared/components/pagination";
import { formatCompactDateTime, formatDateTime, formatNumber } from "@/shared/lib/format";
import { cn } from "@/shared/lib/cn";

const PROVIDER_OPTIONS = [
  { value: "grok", label: "Grok" },
  { value: "chatgpt", label: "ChatGPT" },
  { value: "kiro", label: "Kiro" },
];

const STATUS_OPTIONS = [
  { value: "success", label: "成功" },
  { value: "error", label: "失败" },
];

export function RequestAuditsPage() {
  const { t, i18n } = useTranslation();
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  const auditsQuery = useQuery({
    queryKey: ["gateway-requests", provider, status, page, pageSize],
    queryFn: ({ signal }) => listGatewayRequests({
      provider,
      status,
      limit: pageSize,
      offset: (page - 1) * pageSize,
      signal,
    }),
    placeholderData: (previous) => previous,
  });

  const result = auditsQuery.data;
  const items = result?.items ?? [];
  const total = result?.total ?? 0;

  function changeProvider(value: string): void {
    setProvider(value);
    setPage(1);
  }

  function changeStatus(value: string): void {
    setStatus(value);
    setPage(1);
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("audits.title")}
        description="查看经由网关转发的请求记录，包含路由、状态、耗时与 token 用量。"
        actions={(
          <Button variant="secondary" size="sm" onClick={() => void auditsQuery.refetch()} disabled={auditsQuery.isFetching}>
            <RefreshCw className={auditsQuery.isFetching ? "animate-spin" : undefined} />{t("common.refresh")}
          </Button>
        )}
      />

      <DataTableShell
        toolbar={(
          <div className="flex w-full flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <DataTableFilters filters={[
                { id: "provider", label: "Provider", value: provider, onChange: changeProvider, options: PROVIDER_OPTIONS },
                { id: "status", label: t("audits.status"), value: status, onChange: changeStatus, options: STATUS_OPTIONS },
              ]} />
              <span className="text-xs text-muted-foreground">共 {formatNumber(total, i18n.language, 0)} 条</span>
            </div>
          </div>
        )}
        footer={total > 0 ? (
          <Pagination
            page={page}
            pageSize={pageSize}
            total={total}
            onPageChange={setPage}
            onPageSizeChange={(value) => { setPageSize(value); setPage(1); }}
          />
        ) : undefined}
      >
        {auditsQuery.isError ? <ErrorState message={auditsQuery.error.message} onRetry={() => void auditsQuery.refetch()} /> : null}
        {!auditsQuery.isError && result && items.length === 0 ? <EmptyState message="暂无请求记录" /> : null}
        {!auditsQuery.isError && (auditsQuery.isPending || items.length > 0) ? (
          <Table className="min-w-[1180px] text-xs">
            <TableHeader>
              <TableRow>
                <TableHead className="w-40">{t("audits.createdAt")}</TableHead>
                <TableHead className="w-20">Provider</TableHead>
                <TableHead className="w-52">{t("audits.model")}</TableHead>
                <TableHead className="w-20">能力</TableHead>
                <TableHead className="w-40">{t("audits.account")}</TableHead>
                <TableHead className="w-24 text-center">{t("audits.status")}</TableHead>
                <TableHead className="w-24 text-right">{t("audits.duration")}</TableHead>
                <TableHead className="w-20 text-center">流式</TableHead>
                <TableHead className="w-40">Tokens</TableHead>
                <TableHead>{t("audits.key")} / 错误</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {auditsQuery.isPending ? (
                <TableLoadingRow colSpan={10} />
              ) : (
                items.map((request) => <AuditRow key={request.id} request={request} locale={i18n.language} />)
              )}
            </TableBody>
          </Table>
        ) : null}
      </DataTableShell>
    </div>
  );
}

function AuditRow({ request, locale }: { request: GatewayRequestDTO; locale: string }) {
  const failed = request.status !== "success" || request.httpStatus >= 400;
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap text-muted-foreground tabular-nums">
        <time dateTime={new Date(request.createdAt).toISOString()} title={formatDateTime(new Date(request.createdAt).toISOString(), locale)}>
          {formatCompactDateTime(new Date(request.createdAt).toISOString(), locale)}
        </time>
      </TableCell>
      <TableCell><ProviderBadge provider={request.provider} /></TableCell>
      <TableCell className="min-w-0">
        <span className="block truncate font-medium" title={request.model || "-"}>{request.model || "-"}</span>
        <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground" title={request.id}>{request.id}</span>
      </TableCell>
      <TableCell><Badge variant="outline">{request.capability || "-"}</Badge></TableCell>
      <TableCell className="max-w-40">
        <span className="block truncate text-muted-foreground" title={request.accountId || "-"}>{request.accountId || "-"}</span>
      </TableCell>
      <TableCell className="text-center">
        <Badge
          variant="secondary"
          className={cn(
            "min-w-9 justify-center tabular-nums",
            failed ? "bg-red-500/10 text-red-700 dark:text-red-300" : "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
          )}
        >
          {request.httpStatus || "-"}
        </Badge>
      </TableCell>
      <TableCell className="text-right tabular-nums text-muted-foreground">{formatNumber(request.latencyMs, locale, 0)} ms</TableCell>
      <TableCell className="text-center text-muted-foreground">{request.stream ? "流式" : "非流式"}</TableCell>
      <TableCell className="whitespace-nowrap tabular-nums text-muted-foreground">
        ↑ {formatNumber(request.promptTokens, locale, 0)} · ↓ {formatNumber(request.completionTokens, locale, 0)}
      </TableCell>
      <TableCell className="max-w-64">
        <div className="truncate font-mono text-[10px] text-muted-foreground" title={request.clientKeyId || "-"}>{request.clientKeyId || "-"}</div>
        {request.error ? <div className="mt-0.5 truncate text-[11px] text-destructive" title={request.error}>{request.error}</div> : null}
      </TableCell>
    </TableRow>
  );
}

function ProviderBadge({ provider }: { provider: string }) {
  const label = PROVIDER_OPTIONS.find((option) => option.value === provider)?.label ?? (provider || "-");
  return <Badge variant="outline">{label}</Badge>;
}
