import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import {
  createGatewayKey,
  deleteGatewayKey,
  formatGatewayTimestamp,
  getDefaultGatewayKey,
  listGatewayKeys,
  listGatewayModels,
  type CreatedGatewayKeyDTO,
  type GatewayKeyDTO,
  type GatewayScope,
} from "@/shared/api/gateway";
import { CopyButton } from "@/shared/components/copy-button";
import { PageHeader } from "@/shared/components/page-header";
import { cn } from "@/shared/lib/cn";

const scopeLabels: Record<GatewayScope, string> = {
  chat: "聊天",
  image: "图像",
  video: "视频",
};

export function ClientKeysPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<GatewayScope[]>(["chat"]);
  const [models, setModels] = useState<string[]>([]);
  const [createdKey, setCreatedKey] = useState<CreatedGatewayKeyDTO | null>(null);
  const [deleting, setDeleting] = useState<GatewayKeyDTO | null>(null);
  const [showDefaultKey, setShowDefaultKey] = useState(false);

  const keysQuery = useQuery({ queryKey: ["gateway-keys"], queryFn: listGatewayKeys });
  const defaultKeyQuery = useQuery({ queryKey: ["gateway-default-key"], queryFn: getDefaultGatewayKey });
  const modelsQuery = useQuery({
    queryKey: ["gateway-models"],
    queryFn: () => listGatewayModels(),
    enabled: createOpen,
    staleTime: 60_000,
  });

  const keys = keysQuery.data ?? [];
  const keyword = search.trim().toLocaleLowerCase();
  const visibleKeys = keys
    .filter((key) => (statusFilter === "active" ? key.enabled : statusFilter === "disabled" ? !key.enabled : true))
    .filter((key) => !keyword || key.name.toLocaleLowerCase().includes(keyword) || key.keyPrefix.toLocaleLowerCase().includes(keyword));

  const createMutation = useMutation({
    mutationFn: () => createGatewayKey({ name: name.trim(), scopes, models }),
    onSuccess: (key) => {
      setCreateOpen(false);
      setCreatedKey(key);
      setName("");
      setScopes(["chat"]);
      setModels([]);
      void queryClient.invalidateQueries({ queryKey: ["gateway-keys"] });
      toast.success("密钥已创建");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteGatewayKey(id),
    onSuccess: () => {
      setDeleting(null);
      void queryClient.invalidateQueries({ queryKey: ["gateway-keys"] });
      toast.success("密钥已删除");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  function toggleScope(scope: GatewayScope, checked: boolean): void {
    setScopes((current) => (checked ? [...new Set([...current, scope])] : current.filter((item) => item !== scope)));
  }

  function toggleModel(model: string, checked: boolean): void {
    setModels((current) => (checked ? [...new Set([...current, model])] : current.filter((item) => item !== model)));
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("keys.title")}
        description={t("keys.description")}
        actions={(
          <>
            <Button variant="secondary" size="sm" onClick={() => void keysQuery.refetch()} disabled={keysQuery.isFetching}>
              <RefreshCw className={keysQuery.isFetching ? "animate-spin" : undefined} />{t("common.refresh")}
            </Button>
            <Button size="sm" onClick={() => setCreateOpen(true)}><Plus />{t("keys.create")}</Button>
          </>
        )}
      />

      {defaultKeyQuery.data ? (
        <DashboardPanel id="gateway-default-key" title={t("keys.defaultTitle")}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium">{defaultKeyQuery.data.name}</span>
                <Badge variant="default">{t("keys.defaultBadge")}</Badge>
              </div>
              <code className="mt-2 block break-all rounded-md border border-border/60 bg-secondary/40 px-2.5 py-1.5 font-mono text-xs text-muted-foreground">
                {showDefaultKey ? defaultKeyQuery.data.key : `${defaultKeyQuery.data.keyPrefix}${"•".repeat(24)}`}
              </code>
              <p className="mt-1.5 text-xs text-muted-foreground">{t("keys.defaultHelp")}</p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Button variant="secondary" size="sm" onClick={() => setShowDefaultKey((value) => !value)}>
                {showDefaultKey ? <EyeOff /> : <Eye />}
                {showDefaultKey ? t("keys.hide") : t("keys.reveal")}
              </Button>
              <CopyButton value={defaultKeyQuery.data.key} copyLabel={t("common.copy")} />
            </div>
          </div>
        </DashboardPanel>
      ) : null}

      <DashboardPanel id="gateway-keys-filters" title={t("common.filter")}>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-56 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-8" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t("keys.search")} aria-label={t("keys.search")} />
          </div>
          <Tabs value={statusFilter} onValueChange={setStatusFilter}>
            <TabsList>
              <TabsTrigger value="all">{t("common.all")}</TabsTrigger>
              <TabsTrigger value="active">{t("keys.statusActive")}</TabsTrigger>
              <TabsTrigger value="disabled">{t("common.disabled")}</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
      </DashboardPanel>

      <DashboardPanel id="gateway-keys-list" title={`密钥 ${keys.length}`}>
        {keysQuery.isLoading ? (
          <div className="flex justify-center py-10"><Spinner className="size-5" /></div>
        ) : keysQuery.isError ? (
          <div className="space-y-3 py-6 text-center">
            <p className="text-sm text-destructive">{keysQuery.error instanceof Error ? keysQuery.error.message : t("errors.generic")}</p>
            <Button variant="secondary" size="sm" onClick={() => void keysQuery.refetch()}>{t("common.retry")}</Button>
          </div>
        ) : visibleKeys.length === 0 ? (
          <div className="py-10 text-center text-sm text-muted-foreground">{keys.length === 0 ? "还没有密钥，点击右上角创建" : t("common.noData")}</div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("keys.name")}</TableHead>
                <TableHead>{t("keys.prefix")}</TableHead>
                <TableHead>权限范围</TableHead>
                <TableHead>{t("keys.models")}</TableHead>
                <TableHead>{t("keys.status")}</TableHead>
                <TableHead>{t("keys.lastUsed")}</TableHead>
                <TableHead className="w-12 text-right">{t("common.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleKeys.map((key) => (
                <TableRow key={key.id}>
                  <TableCell>
                    <div className="font-medium">{key.name}</div>
                    <div className="text-xs text-muted-foreground">创建于 {formatGatewayTimestamp(key.createdAt, i18n.language)}</div>
                  </TableCell>
                  <TableCell>
                    <code className="rounded bg-muted px-1.5 py-1 text-xs text-muted-foreground">{key.keyPrefix}••••</code>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {(key.scopes.length > 0 ? key.scopes : ["chat"]).map((scope) => (
                        <Badge key={scope} variant="outline">{scopeLabels[scope as GatewayScope] ?? scope}</Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-64">
                    {key.models.length === 0 ? (
                      <span className="text-xs text-muted-foreground">{t("keys.allModels")}</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {key.models.slice(0, 2).map((model) => <Badge key={model} variant="secondary" className="font-normal">{model}</Badge>)}
                        {key.models.length > 2 ? <Badge variant="outline">+{key.models.length - 2}</Badge> : null}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={key.enabled ? "secondary" : "outline"}
                      className={cn(key.enabled && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300")}
                    >
                      {key.enabled ? t("keys.statusActive") : t("common.disabled")}
                    </Badge>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {formatGatewayTimestamp(key.lastUsedAt, i18n.language)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-8 text-muted-foreground hover:text-destructive"
                      aria-label={t("common.delete")}
                      onClick={() => setDeleting(key)}
                    >
                      <Trash2 />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </DashboardPanel>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="flex max-h-[calc(100svh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-[560px]">
          <DialogHeader className="shrink-0 px-5 py-4 pr-12">
            <DialogTitle>{t("keys.createTitle")}</DialogTitle>
            <DialogDescription>为下游客户端创建调用密钥，明文仅在创建后展示一次。</DialogDescription>
          </DialogHeader>
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 pb-4 pt-2">
            <div className="space-y-1.5">
              <Label htmlFor="gateway-key-name">{t("keys.name")}</Label>
              <Input id="gateway-key-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：生产环境" />
            </div>
            <div className="space-y-2">
              <Label>权限范围</Label>
              <div className="grid gap-2 sm:grid-cols-3">
                {(Object.keys(scopeLabels) as GatewayScope[]).map((scope) => (
                  <label key={scope} htmlFor={`gateway-scope-${scope}`} className={cn("flex cursor-pointer items-center gap-2 rounded-lg border border-border/70 px-3 py-2 text-sm transition-colors", scopes.includes(scope) && "border-primary/40 bg-secondary/60")}>
                    <Checkbox id={`gateway-scope-${scope}`} checked={scopes.includes(scope)} onCheckedChange={(checked) => toggleScope(scope, checked === true)} />
                    {scopeLabels[scope]}
                  </label>
                ))}
              </div>
              {scopes.length === 0 ? <p className="text-xs text-destructive">至少选择一个权限范围</p> : null}
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <Label>模型白名单</Label>
                <span className="text-xs text-muted-foreground">{models.length === 0 ? "留空表示允许全部模型" : `已选择 ${models.length} 个`}</span>
              </div>
              <div className="max-h-52 overflow-y-auto rounded-lg border border-border/70 p-1">
                {modelsQuery.isLoading ? (
                  <div className="flex justify-center py-6"><Spinner className="size-4" /></div>
                ) : (modelsQuery.data ?? []).length === 0 ? (
                  <p className="px-3 py-6 text-center text-xs text-muted-foreground">{t("common.noData")}</p>
                ) : (
                  (modelsQuery.data ?? []).map((model) => (
                    <label key={`${model.provider}:${model.publicModel}`} htmlFor={`gateway-model-${model.provider}-${model.publicModel}`} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-accent/40">
                      <Checkbox id={`gateway-model-${model.provider}-${model.publicModel}`} checked={models.includes(model.publicModel)} onCheckedChange={(checked) => toggleModel(model.publicModel, checked === true)} />
                      <span className="min-w-0 flex-1 truncate">{model.publicModel}</span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">{model.provider}</span>
                      {!model.enabled ? <Badge variant="outline" className="shrink-0 text-[10px]">{t("common.disabled")}</Badge> : null}
                    </label>
                  ))
                )}
              </div>
            </div>
          </div>
          <DialogFooter className="shrink-0 border-t border-border/60 bg-muted/20 px-5 py-3">
            <Button variant="secondary" size="sm" onClick={() => setCreateOpen(false)}>{t("common.cancel")}</Button>
            <Button size="sm" disabled={!name.trim() || scopes.length === 0 || createMutation.isPending} onClick={() => createMutation.mutate()}>
              {createMutation.isPending ? <Spinner /> : null}{t("common.create")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={createdKey !== null} onOpenChange={(open) => !open && setCreatedKey(null)}>
        <DialogContent className="sm:max-w-[480px]">
          <DialogHeader>
            <DialogTitle>{t("keys.secretTitle")}</DialogTitle>
            <DialogDescription>请立即复制并妥善保存，关闭后将无法再次查看明文。</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label>{t("keys.secretLabel")}</Label>
            <div className="flex h-9 w-full min-w-0 overflow-hidden rounded-md border border-input bg-secondary/55">
              <code className="flex min-w-0 flex-1 select-all items-center overflow-x-auto whitespace-nowrap px-3 font-mono text-xs">{createdKey?.key ?? ""}</code>
              <CopyButton value={createdKey?.key ?? ""} className="h-full w-9 shrink-0 rounded-none border-l" copyLabel={t("keys.copySecret")} onCopied={() => toast.success(t("common.copied"))} />
            </div>
          </div>
          <DialogFooter>
            <Button size="sm" onClick={() => setCreatedKey(null)}>{t("common.close")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("keys.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("keys.deleteDescription")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
              onClick={() => deleting && deleteMutation.mutate(deleting.id)}
            >
              {t("common.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
