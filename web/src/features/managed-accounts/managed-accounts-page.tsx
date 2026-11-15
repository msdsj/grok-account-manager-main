import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Pencil, RefreshCw, Search, Tags, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
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
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import {
  deleteManagedAccounts,
  exportManagedAccounts,
  listAccountTags,
  listManagedAccounts,
  refreshManagedAccountQuota,
  updateAccountTags,
  type ManagedAccount,
} from "@/features/managed-accounts/managed-accounts-api";
import { useDebouncedValue } from "@/shared/hooks/use-debounced-value";
import { formatNumber } from "@/shared/lib/format";
import { PageHeader } from "@/shared/components/page-header";

function providerLabel(provider: string, t: (key: string) => string): string {
  if (provider === "chatgpt") return t("managedAccounts.providerGpt");
  if (provider === "kiro") return t("managedAccounts.providerKiro");
  return t("managedAccounts.providerGrok");
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

export function ManagedAccountsPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [provider, setProvider] = useState("all");
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState("all");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [tagDialogKeys, setTagDialogKeys] = useState<string[] | null>(null);
  const [tagValue, setTagValue] = useState("");
  const [deleteKeys, setDeleteKeys] = useState<string[] | null>(null);
  const debouncedSearch = useDebouncedValue(search, 250);

  const accountsQuery = useQuery({
    queryKey: ["managed-accounts", provider, debouncedSearch, tagFilter],
    queryFn: () =>
      listManagedAccounts({
        provider: provider === "all" ? "" : provider,
        search: debouncedSearch,
        tag: tagFilter === "all" ? "" : tagFilter,
      }),
  });
  const tagsQuery = useQuery({
    queryKey: ["managed-account-tags"],
    queryFn: listAccountTags,
  });

  const accounts = accountsQuery.data?.accounts ?? [];
  const tags = tagsQuery.data ?? [];
  const selectedKeys = useMemo(() => Array.from(selected), [selected]);

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
    setSelected(checked ? new Set(accounts.map((account) => account.exportKey)) : new Set());
  };

  const openTagDialog = (keys: string[]) => {
    if (keys.length === 0) {
      toast.error(t("managedAccounts.noSelection"));
      return;
    }
    setTagDialogKeys(keys);
    const first = accounts.find((account) => account.exportKey === keys[0]);
    setTagValue((first?.tags ?? []).join(", "));
  };

  const selectedAll = accounts.length > 0 && accounts.every((account) => selected.has(account.exportKey));

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

      <DashboardPanel id="managed-accounts-filters" title={t("common.filter")}>
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
          <Select value={provider} onValueChange={setProvider}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("managedAccounts.providerAll")}</SelectItem>
              <SelectItem value="grok">{t("managedAccounts.providerGrok")}</SelectItem>
              <SelectItem value="chatgpt">{t("managedAccounts.providerGpt")}</SelectItem>
              <SelectItem value="kiro">{t("managedAccounts.providerKiro")}</SelectItem>
            </SelectContent>
          </Select>
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
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{t("managedAccounts.total", { count: accountsQuery.data?.total ?? 0 })}</span>
          {selectedKeys.length > 0 ? <span>{t("managedAccounts.selected", { count: selectedKeys.length })}</span> : null}
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

      <DashboardPanel id="managed-accounts-list" title={t("managedAccounts.total", { count: accountsQuery.data?.total ?? 0 })}>
        {accountsQuery.isLoading ? (
          <div className="flex justify-center py-10"><Spinner className="size-5" /></div>
        ) : accountsQuery.isError ? (
          <p className="py-6 text-sm text-destructive">{t("managedAccounts.loadFailed")}</p>
        ) : accounts.length === 0 ? (
          <p className="py-6 text-sm text-muted-foreground">{t("managedAccounts.noAccounts")}</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">
                  <Checkbox checked={selectedAll} onCheckedChange={(checked) => toggleAll(checked === true)} />
                </TableHead>
                <TableHead>{t("managedAccounts.columns.email")}</TableHead>
                <TableHead>{t("managedAccounts.columns.provider")}</TableHead>
                <TableHead>{t("managedAccounts.columns.status")}</TableHead>
                <TableHead>{t("managedAccounts.columns.quota")}</TableHead>
                <TableHead>{t("managedAccounts.columns.tags")}</TableHead>
                <TableHead className="text-right">{t("managedAccounts.columns.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.map((account) => {
                const status = accountStatus(account);
                const weeklyUsed = account.quota?.weeklyUsed;
                const weeklyTotal = account.quota?.weeklyTotal;
                return (
                  <TableRow key={account.exportKey}>
                    <TableCell>
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
                      <div className="text-xs text-muted-foreground">{account.displayName || account.id}</div>
                    </TableCell>
                    <TableCell><Badge variant="outline">{providerLabel(String(account.provider), t)}</Badge></TableCell>
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
                      <div className="flex justify-end gap-1">
                        <Button size="sm" variant="ghost" onClick={() => openTagDialog([account.exportKey])}>
                          <Pencil />{t("managedAccounts.editTags")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={quotaMutation.isPending}
                          onClick={() => quotaMutation.mutate(account.id)}
                        >
                          <RefreshCw />{t("managedAccounts.refreshQuota")}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </DashboardPanel>

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
    </div>
  );
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
