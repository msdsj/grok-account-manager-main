import { useQuery } from "@tanstack/react-query";
import { Boxes, RefreshCw, Search, Users } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getGatewayStatus, listGatewayModels, type GatewayProviderStatusDTO } from "@/shared/api/gateway";
import { PageHeader } from "@/shared/components/page-header";
import { cn } from "@/shared/lib/cn";

const providerLabels: Record<string, string> = {
  grok: "Grok",
  chatgpt: "ChatGPT",
  kiro: "Kiro",
};

export function ModelsPage() {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [providerFilter, setProviderFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");

  const modelsQuery = useQuery({ queryKey: ["gateway-models"], queryFn: () => listGatewayModels(), staleTime: 30_000 });
  const statusQuery = useQuery({ queryKey: ["gateway-status"], queryFn: () => getGatewayStatus(), staleTime: 30_000 });

  const providers = statusQuery.data?.providers ?? [];
  const providerOptions = useMemo(() => {
    const values = new Set<string>(["grok", "chatgpt", "kiro"]);
    for (const item of providers) values.add(item.provider);
    for (const model of modelsQuery.data ?? []) values.add(model.provider);
    return [...values];
  }, [modelsQuery.data, providers]);

  const models = (modelsQuery.data ?? [])
    .filter((model) => providerFilter === "all" || model.provider === providerFilter)
    .filter((model) => (statusFilter === "enabled" ? model.enabled : statusFilter === "disabled" ? !model.enabled : true))
    .filter((model) => {
      const keyword = search.trim().toLocaleLowerCase();
      if (!keyword) return true;
      return model.publicModel.toLocaleLowerCase().includes(keyword) || model.upstreamModel.toLocaleLowerCase().includes(keyword);
    });

  const refreshing = modelsQuery.isFetching || statusQuery.isFetching;

  function refresh(): void {
    void Promise.all([modelsQuery.refetch(), statusQuery.refetch()]);
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("models.title")}
        description="只读展示网关模型目录与各 provider 的账号、模型数量。"
        actions={(
          <Button variant="secondary" size="sm" onClick={refresh} disabled={refreshing}>
            <RefreshCw className={refreshing ? "animate-spin" : undefined} />{t("common.refresh")}
          </Button>
        )}
      />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="网关状态">
        {statusQuery.isPending ? (
          <div className="flex min-h-24 items-center justify-center rounded-xl border border-border/80 bg-card sm:col-span-2 xl:col-span-3"><Spinner /></div>
        ) : providers.length === 0 ? (
          <div className="rounded-xl border border-border/80 bg-card px-4 py-6 text-center text-sm text-muted-foreground sm:col-span-2 xl:col-span-3">
            {statusQuery.isError ? "网关状态加载失败" : "暂无 provider 数据"}
          </div>
        ) : (
          providers.map((provider) => <ProviderStatusCard key={provider.provider} status={provider} />)
        )}
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input className="pl-8" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索公共模型或上游模型" aria-label="搜索模型" />
        </div>
        <Select value={providerFilter} onValueChange={setProviderFilter}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部 Provider</SelectItem>
            {providerOptions.map((provider) => (
              <SelectItem key={provider} value={provider}>{providerLabels[provider] ?? provider}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Tabs value={statusFilter} onValueChange={setStatusFilter}>
          <TabsList>
            <TabsTrigger value="all">{t("common.all")}</TabsTrigger>
            <TabsTrigger value="enabled">{t("common.enabled")}</TabsTrigger>
            <TabsTrigger value="disabled">{t("common.disabled")}</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      <section className="overflow-hidden rounded-xl border border-border/80 bg-card shadow-[var(--shadow-card)]">
        {modelsQuery.isPending ? (
          <div className="flex min-h-48 items-center justify-center"><Spinner className="size-5" /></div>
        ) : modelsQuery.isError ? (
          <div className="flex min-h-48 flex-col items-center justify-center gap-3 text-center">
            <p className="text-sm text-destructive">{modelsQuery.error.message}</p>
            <Button variant="secondary" size="sm" onClick={() => void modelsQuery.refetch()}>{t("common.retry")}</Button>
          </div>
        ) : models.length === 0 ? (
          <div className="py-12 text-center text-sm text-muted-foreground">{t("common.noData")}</div>
        ) : (
          <Table className="min-w-[760px] text-xs">
            <TableHeader>
              <TableRow>
                <TableHead>公共模型</TableHead>
                <TableHead>Provider</TableHead>
                <TableHead>上游模型</TableHead>
                <TableHead>能力</TableHead>
                <TableHead className="text-center">{t("models.status")}</TableHead>
                <TableHead className="text-right">权重</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.map((model) => (
                <TableRow key={`${model.provider}:${model.publicModel}`}>
                  <TableCell className="min-w-0">
                    <span className="block truncate font-medium" title={model.publicModel}>{model.publicModel}</span>
                  </TableCell>
                  <TableCell><Badge variant="outline">{providerLabels[model.provider] ?? model.provider}</Badge></TableCell>
                  <TableCell className="min-w-0">
                    <span className="block truncate text-muted-foreground" title={model.upstreamModel}>{model.upstreamModel}</span>
                  </TableCell>
                  <TableCell><Badge variant="secondary" className="font-normal">{model.capability}</Badge></TableCell>
                  <TableCell className="text-center">
                    <Badge
                      variant={model.enabled ? "secondary" : "outline"}
                      className={cn(model.enabled && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}
                    >
                      {model.enabled ? t("common.enabled") : t("common.disabled")}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-muted-foreground">{model.weight ?? 1}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>
    </div>
  );
}

function ProviderStatusCard({ status }: { status: GatewayProviderStatusDTO }) {
  return (
    <article className="rounded-xl border border-border/80 bg-card p-4 shadow-[var(--shadow-card)]">
      <header className="flex items-center justify-between gap-3">
        <span className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">{providerLabels[status.provider] ?? status.provider}</span>
        <span className="flex size-6 items-center justify-center rounded-md bg-secondary/70 text-muted-foreground"><Boxes className="size-3.5" strokeWidth={1.8} /></span>
      </header>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <p className="flex items-center gap-1 text-[11px] text-muted-foreground"><Users className="size-3" />账号</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">{status.accounts}</p>
        </div>
        <div>
          <p className="flex items-center gap-1 text-[11px] text-muted-foreground"><Boxes className="size-3" />模型</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">{status.models}</p>
        </div>
      </div>
    </article>
  );
}
