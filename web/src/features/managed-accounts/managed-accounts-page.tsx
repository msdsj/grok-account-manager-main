import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bot,
  Download,
  FlaskConical,
  ImageIcon,
  MessageSquareText,
  Pencil,
  RefreshCw,
  Search,
  Sparkles,
  Tags,
  Trash2,
  Video,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import {
  deleteManagedAccounts,
  exportManagedAccounts,
  getManagedAccountVideoStatus,
  listAccountTags,
  listManagedAccounts,
  refreshManagedAccountQuota,
  testManagedAccountChat,
  testManagedAccountImage,
  testManagedAccountVideo,
  updateAccountTags,
  type AccountTestMode,
  type AccountTestResult,
  type ManagedAccount,
} from "@/features/managed-accounts/managed-accounts-api";
import { listGatewayModels, type GatewayModelDTO } from "@/shared/api/gateway";
import { PageHeader } from "@/shared/components/page-header";
import { useDebouncedValue } from "@/shared/hooks/use-debounced-value";
import { formatNumber } from "@/shared/lib/format";
import { cn } from "@/shared/lib/cn";

type PoolTab = {
  key: "grok" | "kiro" | "gpt";
  provider: string;
  labelKey: string;
  descriptionKey: string;
  icon: LucideIcon;
  modes: readonly AccountTestMode[];
};

type PoolTabKey = PoolTab["key"];

const POOL_TABS: readonly PoolTab[] = [
  {
    key: "grok",
    provider: "grok",
    labelKey: "managedAccounts.tabs.grok",
    descriptionKey: "managedAccounts.tabDescriptions.grok",
    icon: Sparkles,
    modes: ["chat", "image", "video"],
  },
  {
    key: "kiro",
    provider: "kiro",
    labelKey: "managedAccounts.tabs.kiro",
    descriptionKey: "managedAccounts.tabDescriptions.kiro",
    icon: Bot,
    modes: ["chat"],
  },
  {
    key: "gpt",
    provider: "chatgpt",
    labelKey: "managedAccounts.tabs.gpt",
    descriptionKey: "managedAccounts.tabDescriptions.gpt",
    icon: ImageIcon,
    modes: ["image"],
  },
];

const TEST_MODES: readonly AccountTestMode[] = ["chat", "image", "video"];

const TEST_MODE_META: Record<AccountTestMode, { icon: LucideIcon; labelKey: string; promptKey: string }> = {
  chat: { icon: MessageSquareText, labelKey: "managedAccounts.testDialog.modes.chat", promptKey: "managedAccounts.testDialog.chatPrompt" },
  image: { icon: ImageIcon, labelKey: "managedAccounts.testDialog.modes.image", promptKey: "managedAccounts.testDialog.imagePrompt" },
  video: { icon: Video, labelKey: "managedAccounts.testDialog.modes.video", promptKey: "managedAccounts.testDialog.videoPrompt" },
};

const FALLBACK_MODELS: Record<string, Record<AccountTestMode, string[]>> = {
  grok: {
    chat: ["grok-4.5", "grok-4.20-auto", "grok-4.20-expert"],
    image: ["grok-imagine-image", "grok-imagine-image-quality"],
    video: ["grok-imagine-video-1.5", "grok-imagine-video"],
  },
  kiro: {
    chat: ["kiro-auto", "kiro-claude-sonnet-4.5", "kiro-claude-opus-4"],
    image: [],
    video: [],
  },
  chatgpt: {
    chat: [],
    image: ["gpt-image-1", "gpt-image-1-mini", "gpt-image-2"],
    video: [],
  },
};

function normalizeProvider(provider: string): string {
  const value = String(provider || "").toLowerCase();
  if (value === "gpt" || value === "openai") return "chatgpt";
  return value;
}

function providerSupports(provider: string, mode: AccountTestMode): boolean {
  return POOL_TABS.find((tab) => tab.provider === normalizeProvider(provider))?.modes.includes(mode) ?? false;
}

function providerLabel(provider: string, t: (key: string) => string): string {
  const normalized = normalizeProvider(provider);
  if (normalized === "chatgpt") return t("managedAccounts.providerGpt");
  if (normalized === "kiro") return t("managedAccounts.providerKiro");
  return t("managedAccounts.providerGrok");
}

function capabilityTested(account: ManagedAccount, mode: AccountTestMode): boolean {
  if (mode === "chat") return Boolean(account.availability?.chatAvailable);
  if (mode === "image") return Boolean(account.availability?.imageAvailable);
  return Boolean(account.availability?.videoAvailable);
}

function accountStatus(account: ManagedAccount): "active" | "limited" | "unknown" {
  if (account.oauthError || account.availability?.error) return "limited";
  const status = String(account.oauthStatus || "").toLowerCase();
  if (status === "ready" || status === "active" || status === "not_requested") return "active";
  if (status) return "limited";
  return "unknown";
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function parseTags(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function readString(source: Record<string, unknown> | undefined, ...keys: string[]): string {
  if (!source) return "";
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function videoInfo(payload: Record<string, unknown> | undefined): { id: string; status: string; url: string } {
  const video = payload && typeof payload.video === "object" && payload.video !== null
    ? (payload.video as Record<string, unknown>)
    : undefined;
  return {
    id: readString(payload, "request_id", "id", "video_id") || readString(video, "id", "video_id"),
    status: readString(payload, "status") || readString(video, "status"),
    url: readString(payload, "video_url", "url") || readString(video, "url", "video_url"),
  };
}

function imageSource(image: { url?: string; b64_json?: string }): string {
  if (image.url) return image.url;
  if (image.b64_json) return `data:image/png;base64,${image.b64_json}`;
  return "";
}

export function ManagedAccountsPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<PoolTabKey>("grok");
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState("all");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [tagDialogKeys, setTagDialogKeys] = useState<string[] | null>(null);
  const [tagValue, setTagValue] = useState("");
  const [deleteKeys, setDeleteKeys] = useState<string[] | null>(null);
  const [testTarget, setTestTarget] = useState<{ account: ManagedAccount; mode: AccountTestMode } | null>(null);
  const debouncedSearch = useDebouncedValue(search, 250);
  const activeTabDef = POOL_TABS.find((tab) => tab.key === activeTab) ?? POOL_TABS[0];

  const accountsQuery = useQuery({
    queryKey: ["managed-accounts", debouncedSearch, tagFilter],
    queryFn: () =>
      listManagedAccounts({
        search: debouncedSearch,
        tag: tagFilter === "all" ? "" : tagFilter,
      }),
  });
  const tagsQuery = useQuery({
    queryKey: ["managed-account-tags"],
    queryFn: listAccountTags,
  });
  const modelsQuery = useQuery({
    queryKey: ["managed-accounts", "models"],
    queryFn: () => listGatewayModels(),
    staleTime: 60_000,
  });

  const allAccounts = accountsQuery.data?.accounts ?? [];
  const tags = tagsQuery.data ?? [];
  const selectedKeys = useMemo(() => Array.from(selected), [selected]);
  const counts = useMemo<Record<PoolTabKey, number>>(
    () => ({
      grok: allAccounts.filter((account) => normalizeProvider(String(account.provider)) === "grok").length,
      kiro: allAccounts.filter((account) => normalizeProvider(String(account.provider)) === "kiro").length,
      gpt: allAccounts.filter((account) => normalizeProvider(String(account.provider)) === "chatgpt").length,
    }),
    [allAccounts],
  );
  const tabAccounts = useMemo(
    () => allAccounts.filter((account) => normalizeProvider(String(account.provider)) === activeTabDef.provider),
    [activeTabDef.provider, allAccounts],
  );

  useEffect(() => {
    setSelected(new Set());
  }, [activeTab]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["managed-accounts"] });
    void queryClient.invalidateQueries({ queryKey: ["managed-account-tags"] });
  };

  const saveTagsMutation = useMutation({
    mutationFn: () => updateAccountTags(tagDialogKeys ?? [], parseTags(tagValue), "replace"),
    onSuccess: () => {
      setTagDialogKeys(null);
      setTagValue("");
      invalidate();
      toast.success(t("managedAccounts.tagsSaved"));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteManagedAccounts(deleteKeys ?? []),
    onSuccess: (result) => {
      setDeleteKeys(null);
      setSelected(new Set());
      invalidate();
      toast.success(t("managedAccounts.deleted", { count: result.deleted }));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const exportMutation = useMutation({
    mutationFn: (keys: string[]) => exportManagedAccounts(keys),
    onSuccess: (blob) => {
      downloadBlob(blob, `managed-accounts-${Date.now()}.json`);
      toast.success(t("managedAccounts.exported", { count: selectedKeys.length }));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const quotaMutation = useMutation({
    mutationFn: (accountId: string) => refreshManagedAccountQuota(accountId),
    onSuccess: () => {
      invalidate();
      toast.success(t("managedAccounts.refreshQuota"));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const toggleAll = (checked: boolean) => {
    setSelected(checked ? new Set(tabAccounts.map((account) => account.exportKey)) : new Set());
  };

  const openTagDialog = (keys: string[]) => {
    if (keys.length === 0) {
      toast.error(t("managedAccounts.noSelection"));
      return;
    }
    setTagDialogKeys(keys);
    const first = allAccounts.find((account) => account.exportKey === keys[0]);
    setTagValue((first?.tags ?? []).join(", "));
  };

  const openTest = (account: ManagedAccount, mode?: AccountTestMode) => {
    const supported = POOL_TABS.find((tab) => tab.provider === normalizeProvider(String(account.provider)))?.modes ?? [];
    const initialMode = mode && supported.includes(mode) ? mode : supported[0] ?? "chat";
    setTestTarget({ account, mode: initialMode });
  };

  const selectedAll = tabAccounts.length > 0 && tabAccounts.every((account) => selected.has(account.exportKey));
  const ActiveIcon = activeTabDef.icon;

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("managedAccounts.title")}
        description={t("managedAccounts.description")}
        actions={(
          <Button variant="secondary" size="sm" onClick={() => void accountsQuery.refetch()} disabled={accountsQuery.isFetching}>
            <RefreshCw />{t("operationLogs.refresh")}
          </Button>
        )}
      />

      <Tabs value={activeTab} onValueChange={(value) => setActiveTab(value as PoolTabKey)}>
        <div className="min-w-0 overflow-x-auto rounded-xl border border-border/80 bg-card p-1.5 shadow-[var(--shadow-card)]">
          <TabsList className="w-full min-w-[280px]">
            {POOL_TABS.map((tab) => {
              const Icon = tab.icon;
              return (
                <TabsTrigger key={tab.key} value={tab.key} className="flex-1">
                  <Icon className="size-3.5" strokeWidth={1.8} />
                  <span className="hidden sm:inline">{t(tab.labelKey)}</span>
                  <span className="sm:hidden">{tab.key === "gpt" ? "GPT" : tab.key === "kiro" ? "Kiro" : "Grok"}</span>
                  <Badge variant="secondary" className="ml-0.5 h-4 min-w-4 px-1 text-[10px] tabular-nums">
                    {counts[tab.key]}
                  </Badge>
                </TabsTrigger>
              );
            })}
          </TabsList>
        </div>

        <TabsContent value={activeTab} className="mt-4 space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3 px-0.5">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <ActiveIcon className="size-4 text-muted-foreground" strokeWidth={1.8} />
                <h2 className="text-[13px] font-semibold">{t(activeTabDef.labelKey)}</h2>
                <Badge variant="outline" className="tabular-nums">{tabAccounts.length}</Badge>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{t(activeTabDef.descriptionKey)}</p>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {TEST_MODES.map((mode) => {
                const Icon = TEST_MODE_META[mode].icon;
                const supported = activeTabDef.modes.includes(mode);
                return (
                  <Badge key={mode} variant={supported ? "default" : "outline"} className={cn(!supported && "opacity-50")}>
                    <Icon className="size-3" />
                    {t(TEST_MODE_META[mode].labelKey)}
                  </Badge>
                );
              })}
            </div>
          </div>

          <DashboardPanel id="managed-accounts-filters" title={t("managedAccounts.filtersTitle")}>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-56 flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-8"
                  value={search}
                  placeholder={t("managedAccounts.searchPlaceholder")}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
              <Select value={tagFilter} onValueChange={setTagFilter}>
                <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t("managedAccounts.tagAll")}</SelectItem>
                  {tags.map((item) => (
                    <SelectItem key={item.tag} value={item.tag}>{`${item.tag} (${item.count})`}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <div className="flex flex-wrap items-center gap-2">
                <span>{t("managedAccounts.total", { count: tabAccounts.length })}</span>
                {selectedKeys.length > 0 ? <span>{t("managedAccounts.selected", { count: selectedKeys.length })}</span> : null}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="secondary" onClick={() => openTagDialog(selectedKeys)}>
                  <Tags />{t("managedAccounts.editTags")}
                </Button>
                <Button size="sm" variant="secondary" disabled={selectedKeys.length === 0 || exportMutation.isPending} onClick={() => exportMutation.mutate(selectedKeys)}>
                  <Download />{t("managedAccounts.exportSelected")}
                </Button>
                <Button size="sm" variant="secondary" disabled={selectedKeys.length === 0} onClick={() => setDeleteKeys(selectedKeys)}>
                  <Trash2 />{t("managedAccounts.deleteSelected")}
                </Button>
              </div>
            </div>
          </DashboardPanel>

          <DashboardPanel id="managed-accounts-list" title={t("managedAccounts.listTitle", { provider: t(activeTabDef.labelKey) })}>
            {accountsQuery.isLoading ? (
              <div className="flex justify-center py-12"><Spinner className="size-5" /></div>
            ) : accountsQuery.isError ? (
              <p className="py-6 text-sm text-destructive">{t("managedAccounts.loadFailed")}</p>
            ) : tabAccounts.length === 0 ? (
              <div className="py-10 text-center">
                <p className="text-sm font-medium">{t("managedAccounts.noAccounts")}</p>
                <p className="mt-1 text-xs text-muted-foreground">{t(activeTabDef.descriptionKey)}</p>
              </div>
            ) : (
              <div className="-mx-4 overflow-x-auto sm:-mx-5">
                <Table className="min-w-[860px]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-10 pl-4 sm:pl-5">
                        <Checkbox checked={selectedAll} onCheckedChange={(checked) => toggleAll(checked === true)} />
                      </TableHead>
                      <TableHead>{t("managedAccounts.columns.email")}</TableHead>
                      <TableHead>{t("managedAccounts.columns.status")}</TableHead>
                      <TableHead>{t("managedAccounts.columns.quota")}</TableHead>
                      <TableHead>{t("managedAccounts.columns.tags")}</TableHead>
                      <TableHead>{t("managedAccounts.columns.tests")}</TableHead>
                      <TableHead className="pr-4 text-right sm:pr-5">{t("managedAccounts.columns.actions")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {tabAccounts.map((account) => {
                      const status = accountStatus(account);
                      const weeklyUsed = account.quota?.weeklyUsed;
                      const weeklyTotal = account.quota?.weeklyTotal;
                      return (
                        <TableRow key={account.exportKey}>
                          <TableCell className="pl-4 sm:pl-5">
                            <Checkbox
                              checked={selected.has(account.exportKey)}
                              onCheckedChange={(checked) => {
                                setSelected((current) => {
                                  const next = new Set(current);
                                  if (checked === true) next.add(account.exportKey);
                                  else next.delete(account.exportKey);
                                  return next;
                                });
                              }}
                            />
                          </TableCell>
                          <TableCell>
                            <div className="font-medium">{account.email}</div>
                            <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                              <span>{account.displayName || account.id}</span>
                              <Badge variant="outline">{providerLabel(String(account.provider), t)}</Badge>
                            </div>
                            <Button size="sm" variant="secondary" className="mt-2 sm:hidden" onClick={() => openTest(account)}>
                              <FlaskConical />{t("managedAccounts.test")}
                            </Button>
                          </TableCell>
                          <TableCell>
                            <Badge variant={status === "active" ? "default" : status === "limited" ? "destructive" : "outline"}>
                              {status === "active" ? t("managedAccounts.statusActive") : status === "limited" ? t("managedAccounts.statusLimited") : t("managedAccounts.statusUnknown")}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {typeof weeklyUsed === "number" && typeof weeklyTotal === "number"
                              ? t("managedAccounts.quotaWeekly", {
                                  used: formatNumber(weeklyUsed, i18n.language, 0),
                                  total: formatNumber(weeklyTotal, i18n.language, 0),
                                })
                              : t("managedAccounts.quotaNone")}
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-wrap gap-1">
                              {(account.tags ?? []).map((tag) => (
                                <button
                                  key={tag}
                                  type="button"
                                  className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                                  onClick={() => setTagFilter(tag)}
                                >
                                  {tag}
                                </button>
                              ))}
                              {(account.tags ?? []).length === 0 ? <span className="text-xs text-muted-foreground">-</span> : null}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-1">
                              {TEST_MODES.map((mode) => {
                                const meta = TEST_MODE_META[mode];
                                const Icon = meta.icon;
                                const supported = providerSupports(String(account.provider), mode);
                                const tested = supported && capabilityTested(account, mode);
                                return (
                                  <button
                                    key={mode}
                                    type="button"
                                    disabled={!supported}
                                    title={supported ? t(meta.labelKey) : t("managedAccounts.testDialog.unsupported")}
                                    onClick={() => openTest(account, mode)}
                                    className={cn(
                                      "inline-flex h-6 items-center gap-1 rounded-md border px-1.5 text-[11px] transition-colors",
                                      supported
                                        ? "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground"
                                        : "cursor-not-allowed border-dashed border-border/60 text-muted-foreground/40",
                                      tested && supported && "border-primary/35 bg-primary/10 text-primary",
                                    )}
                                  >
                                    <Icon className="size-3" />
                                    {t(meta.labelKey)}
                                  </button>
                                );
                              })}
                            </div>
                          </TableCell>
                          <TableCell className="pr-4 sm:pr-5">
                            <div className="flex items-center justify-end gap-1">
                              <Button size="sm" variant="secondary" onClick={() => openTest(account)}>
                                <FlaskConical />{t("managedAccounts.test")}
                              </Button>
                              <Button size="icon" variant="ghost" className="size-7" title={t("managedAccounts.editTags")} onClick={() => openTagDialog([account.exportKey])}>
                                <Pencil />
                              </Button>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="size-7"
                                title={t("managedAccounts.refreshQuota")}
                                disabled={quotaMutation.isPending}
                                onClick={() => quotaMutation.mutate(account.id)}
                              >
                                <RefreshCw />
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </DashboardPanel>
        </TabsContent>
      </Tabs>

      <Dialog open={tagDialogKeys !== null} onOpenChange={(open) => { if (!open) setTagDialogKeys(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("managedAccounts.tagsDialogTitle")}</DialogTitle>
            <DialogDescription>{t("managedAccounts.tagsDialogDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="managed-account-tags">{t("managedAccounts.columns.tags")}</Label>
            <Input
              id="managed-account-tags"
              value={tagValue}
              placeholder={t("managedAccounts.tagsPlaceholder")}
              onChange={(event) => setTagValue(event.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setTagDialogKeys(null)}>{t("managedAccounts.cancel")}</Button>
            <Button onClick={() => saveTagsMutation.mutate()} disabled={saveTagsMutation.isPending}>
              {t("managedAccounts.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={deleteKeys !== null} onOpenChange={(open) => { if (!open) setDeleteKeys(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("managedAccounts.deleteConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("managedAccounts.deleteConfirmDescription", { count: deleteKeys?.length ?? 0 })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("managedAccounts.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => deleteMutation.mutate()}>{t("managedAccounts.deleteSelected")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {testTarget ? (
        <AccountTestDialog
          key={`${testTarget.account.exportKey}:${testTarget.mode}`}
          account={testTarget.account}
          initialMode={testTarget.mode}
          models={modelsQuery.data ?? []}
          onClose={() => setTestTarget(null)}
          onCompleted={invalidate}
        />
      ) : null}
    </div>
  );
}

type AccountTestDialogProps = {
  account: ManagedAccount;
  initialMode: AccountTestMode;
  models: GatewayModelDTO[];
  onClose: () => void;
  onCompleted: () => void;
};

function AccountTestDialog({ account, initialMode, models, onClose, onCompleted }: AccountTestDialogProps) {
  const { t } = useTranslation();
  const provider = normalizeProvider(String(account.provider));
  const supportedModes = POOL_TABS.find((tab) => tab.provider === provider)?.modes ?? [];
  const [mode, setMode] = useState<AccountTestMode>(supportedModes.includes(initialMode) ? initialMode : supportedModes[0] ?? "chat");
  const [selectedModels, setSelectedModels] = useState<Record<AccountTestMode, string>>({ chat: "", image: "", video: "" });
  const [prompt, setPrompt] = useState(() => t(TEST_MODE_META[mode].promptKey));
  const [imageSize, setImageSize] = useState("1024x1024");
  const [imageCount, setImageCount] = useState("1");
  const [videoDuration, setVideoDuration] = useState("6");
  const [videoResolution, setVideoResolution] = useState("720p");
  const [videoAspect, setVideoAspect] = useState("16:9");
  const [result, setResult] = useState<AccountTestResult | null>(null);
  const [errorText, setErrorText] = useState("");

  const modelOptions = useMemo(() => {
    const fromCatalog = models
      .filter((model) => model.enabled && model.provider === provider && model.capability === mode)
      .map((model) => model.publicModel);
    const fallback = FALLBACK_MODELS[provider]?.[mode] ?? [];
    return Array.from(new Set([...fromCatalog, ...fallback]));
  }, [mode, models, provider]);
  const effectiveModel = modelOptions.includes(selectedModels[mode]) ? selectedModels[mode] : modelOptions[0] ?? "";
  const resultVideo = useMemo(() => videoInfo(result?.video), [result]);

  const runMutation = useMutation({
    mutationFn: () => {
      const content = prompt.trim();
      if (!content) throw new Error(t("managedAccounts.testDialog.promptRequired"));
      if (mode === "chat") {
        return testManagedAccountChat({
          exportKey: account.exportKey,
          model: effectiveModel,
          messages: [{ role: "user", content }],
        });
      }
      if (mode === "image") {
        return testManagedAccountImage({
          exportKey: account.exportKey,
          model: effectiveModel,
          prompt: content,
          n: Number(imageCount),
          size: imageSize,
        });
      }
      return testManagedAccountVideo({
        exportKey: account.exportKey,
        model: effectiveModel,
        prompt: content,
        duration: Number(videoDuration),
        resolution: videoResolution,
        aspectRatio: videoAspect,
      });
    },
    onSuccess: (payload) => {
      setResult(payload);
      setErrorText("");
      toast.success(t("managedAccounts.testDialog.success"));
      onCompleted();
    },
    onError: (error) => {
      setResult(null);
      const message = error instanceof Error ? error.message : t("errors.generic");
      setErrorText(message);
      toast.error(t("managedAccounts.testDialog.failed"), { description: message });
    },
  });

  const statusMutation = useMutation({
    mutationFn: () => getManagedAccountVideoStatus({ exportKey: account.exportKey, videoId: resultVideo.id }),
    onSuccess: (payload) => {
      setResult(payload);
      setErrorText("");
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : t("errors.generic");
      setErrorText(message);
      toast.error(t("managedAccounts.testDialog.failed"), { description: message });
    },
  });

  const changeMode = (next: AccountTestMode) => {
    if (!supportedModes.includes(next) || next === mode) return;
    setMode(next);
    setResult(null);
    setErrorText("");
    setPrompt(t(TEST_MODE_META[next].promptKey));
  };

  const isPending = runMutation.isPending || statusMutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            {t("managedAccounts.testDialog.title")}
            <Badge variant="outline">{providerLabel(provider, t)}</Badge>
          </DialogTitle>
          <DialogDescription className="break-all">{account.email}</DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-3 gap-1 rounded-xl bg-secondary/50 p-1" role="tablist" aria-label={t("managedAccounts.testDialog.title")}>
          {TEST_MODES.map((item) => {
            const meta = TEST_MODE_META[item];
            const Icon = meta.icon;
            const supported = supportedModes.includes(item);
            return (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={mode === item}
                disabled={!supported}
                title={supported ? undefined : t("managedAccounts.testDialog.unsupported")}
                onClick={() => changeMode(item)}
                className={cn(
                  "flex h-8 items-center justify-center gap-1.5 rounded-lg text-[13px] transition-colors",
                  mode === item ? "bg-[var(--segment-active)] font-medium text-foreground shadow-[0_1px_2px_rgba(0,0,0,0.08)]" : "text-muted-foreground hover:text-foreground",
                  !supported && "cursor-not-allowed opacity-35 hover:text-muted-foreground",
                )}
              >
                <Icon className="size-3.5" />
                {t(meta.labelKey)}
              </button>
            );
          })}
        </div>

        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>{t("managedAccounts.testDialog.model")}</Label>
              <Select value={effectiveModel} onValueChange={(value) => setSelectedModels((current) => ({ ...current, [mode]: value }))}>
                <SelectTrigger><SelectValue placeholder={t("managedAccounts.testDialog.modelPlaceholder")} /></SelectTrigger>
                <SelectContent>
                  {modelOptions.map((model) => (
                    <SelectItem key={model} value={model}>{model}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {mode === "image" ? (
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t("managedAccounts.testDialog.size")}</Label>
                  <Select value={imageSize} onValueChange={setImageSize}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="1024x1024">1024×1024</SelectItem>
                      <SelectItem value="1536x1024">1536×1024</SelectItem>
                      <SelectItem value="1024x1536">1024×1536</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t("managedAccounts.testDialog.count")}</Label>
                  <Select value={imageCount} onValueChange={setImageCount}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {["1", "2", "3", "4"].map((value) => (
                        <SelectItem key={value} value={value}>{value}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            ) : null}
            {mode === "video" ? (
              <div className="grid grid-cols-3 gap-2">
                <div className="space-y-1.5">
                  <Label>{t("managedAccounts.testDialog.duration")}</Label>
                  <Select value={videoDuration} onValueChange={setVideoDuration}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {["6", "10", "15"].map((value) => (
                        <SelectItem key={value} value={value}>{`${value}s`}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t("managedAccounts.testDialog.resolution")}</Label>
                  <Select value={videoResolution} onValueChange={setVideoResolution}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {["480p", "720p", "1080p"].map((value) => (
                        <SelectItem key={value} value={value}>{value}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t("managedAccounts.testDialog.aspectRatio")}</Label>
                  <Select value={videoAspect} onValueChange={setVideoAspect}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {["16:9", "9:16", "1:1"].map((value) => (
                        <SelectItem key={value} value={value}>{value}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="account-test-prompt">{t("managedAccounts.testDialog.prompt")}</Label>
            <Textarea
              id="account-test-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder={t(TEST_MODE_META[mode].promptKey)}
            />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-[11px] text-muted-foreground">
              {supportedModes.includes(mode) ? t("managedAccounts.testDialog.hint") : t("managedAccounts.testDialog.unsupported")}
            </p>
            <Button size="sm" onClick={() => runMutation.mutate()} disabled={isPending || !effectiveModel}>
              {runMutation.isPending ? <Spinner className="size-3.5" /> : <FlaskConical />}
              {runMutation.isPending ? t("managedAccounts.testDialog.running") : t("managedAccounts.testDialog.run")}
            </Button>
          </div>
        </div>

        <div className="min-h-28 rounded-xl border border-border/70 bg-secondary/25 p-3" aria-live="polite">
          {errorText ? (
            <p className="text-xs leading-5 text-destructive">{errorText}</p>
          ) : !result ? (
            <div className="flex min-h-20 items-center justify-center text-xs text-muted-foreground">
              {t("managedAccounts.testDialog.emptyResult")}
            </div>
          ) : (
            <TestResultView
              mode={mode}
              result={result}
              video={resultVideo}
              refreshing={statusMutation.isPending}
              onRefreshStatus={() => statusMutation.mutate()}
            />
          )}
        </div>

        <DialogFooter>
          <Button variant="secondary" size="sm" onClick={onClose}>{t("common.close")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type TestResultViewProps = {
  mode: AccountTestMode;
  result: AccountTestResult;
  video: { id: string; status: string; url: string };
  refreshing: boolean;
  onRefreshStatus: () => void;
};

function TestResultView({ mode, result, video, refreshing, onRefreshStatus }: TestResultViewProps) {
  const { t } = useTranslation();
  const latency = result.account?.availability?.latencyMs;
  const images = (result.data ?? []).map((image) => ({ ...image, src: imageSource(image) })).filter((image) => image.src);
  const status = video.status.toLowerCase();
  const completed = ["completed", "succeeded", "success", "done"].includes(status);
  const failed = ["failed", "error", "cancelled"].includes(status);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        {result.model ? <Badge variant="outline">{result.model}</Badge> : null}
        {typeof latency === "number" ? <span>{t("managedAccounts.testDialog.latency", { ms: latency })}</span> : null}
      </div>

      {mode === "chat" ? (
        <div className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-border/60 bg-card p-3 text-[13px] leading-6">
          {result.message?.content || t("managedAccounts.testDialog.emptyMessage")}
        </div>
      ) : null}

      {mode === "image" ? (
        images.length > 0 ? (
          <div className="grid gap-3 sm:grid-cols-2">
            {images.map((image, index) => (
              <a key={`${image.src.slice(0, 32)}-${index}`} href={image.src} target="_blank" rel="noreferrer" className="group overflow-hidden rounded-lg border border-border/60 bg-card">
                <img src={image.src} alt={image.revised_prompt || `image-${index + 1}`} className="aspect-square w-full object-cover transition-transform duration-200 group-hover:scale-[1.02]" loading="lazy" />
              </a>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">{t("managedAccounts.testDialog.emptyImages")}</p>
        )
      ) : null}

      {mode === "video" ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge variant={completed ? "default" : failed ? "destructive" : "outline"}>
              {video.status || t("managedAccounts.testDialog.pending")}
            </Badge>
            {video.id ? <span className="font-mono text-[11px] text-muted-foreground">{video.id}</span> : null}
            {video.id ? (
              <Button size="sm" variant="secondary" onClick={onRefreshStatus} disabled={refreshing}>
                {refreshing ? <Spinner className="size-3.5" /> : <RefreshCw />}
                {t("managedAccounts.testDialog.refreshStatus")}
              </Button>
            ) : null}
          </div>
          {video.url ? (
            <video src={video.url} controls className="max-h-80 w-full rounded-lg border border-border/60 bg-black" />
          ) : (
            <p className="text-xs text-muted-foreground">{t("managedAccounts.testDialog.videoPendingHint")}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}
