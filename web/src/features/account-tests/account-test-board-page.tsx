import { useQuery } from "@tanstack/react-query";
import { Bot, FlaskConical, ImageIcon, MessageSquareText, Sparkles, Video, type LucideIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import { AccountTestDialog } from "@/features/managed-accounts/managed-accounts-page";
import { listManagedAccounts, type AccountTestMode, type ManagedAccount } from "@/features/managed-accounts/managed-accounts-api";
import { listGatewayModels } from "@/shared/api/gateway";
import { PageHeader } from "@/shared/components/page-header";
import { formatNumber } from "@/shared/lib/format";

type TestTab = {
  key: "grok" | "kiro" | "gpt";
  provider: string;
  labelKey: string;
  icon: LucideIcon;
  modes: readonly AccountTestMode[];
};

const TABS: readonly TestTab[] = [
  { key: "grok", provider: "grok", labelKey: "managedAccounts.tabs.grok", icon: Sparkles, modes: ["chat", "image", "video"] },
  { key: "kiro", provider: "kiro", labelKey: "managedAccounts.tabs.kiro", icon: Bot, modes: ["chat"] },
  { key: "gpt", provider: "chatgpt", labelKey: "managedAccounts.tabs.gpt", icon: ImageIcon, modes: ["image"] },
];

const MODE_META: Record<AccountTestMode, { icon: LucideIcon; labelKey: string }> = {
  chat: { icon: MessageSquareText, labelKey: "managedAccounts.testDialog.modes.chat" },
  image: { icon: ImageIcon, labelKey: "managedAccounts.testDialog.modes.image" },
  video: { icon: Video, labelKey: "managedAccounts.testDialog.modes.video" },
};

function normalizeProvider(provider: string): string {
  const value = String(provider || "").toLowerCase();
  return value === "gpt" || value === "openai" ? "chatgpt" : value;
}

export function AccountTestBoardPage() {
  const { t, i18n } = useTranslation();
  const [tabKey, setTabKey] = useState<TestTab["key"]>("grok");
  const [target, setTarget] = useState<{ account: ManagedAccount; mode: AccountTestMode } | null>(null);
  const activeTab = TABS.find((tab) => tab.key === tabKey) ?? TABS[0];

  const accountsQuery = useQuery({
    queryKey: ["account-test-board", activeTab.provider],
    queryFn: () => listManagedAccounts({ provider: activeTab.provider }),
  });
  const modelsQuery = useQuery({
    queryKey: ["account-test-board", "models"],
    queryFn: () => listGatewayModels(),
    staleTime: 60_000,
  });
  const accounts = useMemo(
    () => (accountsQuery.data?.accounts ?? []).filter((account) => normalizeProvider(String(account.provider)) === activeTab.provider),
    [accountsQuery.data?.accounts, activeTab.provider],
  );

  return (
    <div className="space-y-5">
      <PageHeader title={t("accountTests.title")} description={t("accountTests.description")} />

      <Tabs value={tabKey} onValueChange={(value) => setTabKey(value as TestTab["key"])}>
        <div className="min-w-0 overflow-x-auto rounded-xl border border-border/80 bg-card p-1.5 shadow-[var(--shadow-card)]">
          <TabsList className="w-full min-w-[280px]">
            {TABS.map((tab) => {
              const Icon = tab.icon;
              return (
                <TabsTrigger key={tab.key} value={tab.key} className="flex-1">
                  <Icon className="size-3.5" strokeWidth={1.8} />
                  <span className="hidden sm:inline">{t(tab.labelKey)}</span>
                  <span className="sm:hidden">{tab.labelKey.endsWith("gpt") ? "GPT" : tab.labelKey.endsWith("kiro") ? "Kiro" : "Grok"}</span>
                </TabsTrigger>
              );
            })}
          </TabsList>
        </div>

        <TabsContent value={tabKey} className="mt-4 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/70 bg-secondary/30 px-4 py-3">
            <div>
              <p className="text-[13px] font-medium">{t("accountTests.activePool", { provider: t(activeTab.labelKey) })}</p>
              <p className="mt-1 text-xs text-muted-foreground">{t("accountTests.useQuotaHint")}</p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {activeTab.modes.map((mode) => {
                const Icon = MODE_META[mode].icon;
                return (
                  <Badge key={mode} variant="outline">
                    <Icon className="size-3" />
                    {t(MODE_META[mode].labelKey)}
                  </Badge>
                );
              })}
            </div>
          </div>

          <DashboardPanel id={`account-tests-${tabKey}`} title={t("accountTests.poolAccounts", { count: accounts.length })}>
            {accountsQuery.isLoading ? (
              <div className="flex justify-center py-12"><Spinner className="size-5" /></div>
            ) : accountsQuery.isError ? (
              <p className="py-6 text-sm text-destructive">{t("managedAccounts.loadFailed")}</p>
            ) : accounts.length === 0 ? (
              <div className="py-10 text-center">
                <p className="text-sm font-medium">{t("managedAccounts.noAccounts")}</p>
                <p className="mt-1 text-xs text-muted-foreground">{t("accountTests.importHint")}</p>
              </div>
            ) : (
              <div className="-mx-4 overflow-x-auto sm:-mx-5">
                <Table className="min-w-[760px]">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4 sm:pl-5">{t("managedAccounts.columns.email")}</TableHead>
                      <TableHead>{t("managedAccounts.columns.quota")}</TableHead>
                      <TableHead>{t("managedAccounts.columns.status")}</TableHead>
                      <TableHead className="pr-4 text-right sm:pr-5">{t("managedAccounts.columns.tests")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {accounts.map((account) => {
                      const weeklyUsed = account.quota?.weeklyUsed;
                      const weeklyTotal = account.quota?.weeklyTotal;
                      return (
                        <TableRow key={account.exportKey}>
                          <TableCell className="pl-4 sm:pl-5">
                            <div className="font-medium">{account.email}</div>
                            <div className="mt-0.5 text-xs text-muted-foreground">{account.displayName || account.id}</div>
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
                            <Badge variant={account.availability?.error ? "destructive" : "outline"}>
                              {account.availability?.error ? t("managedAccounts.statusLimited") : t("managedAccounts.statusActive")}
                            </Badge>
                          </TableCell>
                          <TableCell className="pr-4 sm:pr-5">
                            <div className="flex flex-wrap items-center justify-end gap-1">
                              {activeTab.modes.map((mode) => {
                                const meta = MODE_META[mode];
                                const Icon = meta.icon;
                                const tested = mode === "chat"
                                  ? account.availability?.chatAvailable
                                  : mode === "image"
                                    ? account.availability?.imageAvailable
                                    : account.availability?.videoAvailable;
                                return (
                                  <Button
                                    key={mode}
                                    size="sm"
                                    variant={tested ? "default" : "secondary"}
                                    onClick={() => setTarget({ account, mode })}
                                  >
                                    <Icon />{t(meta.labelKey)}
                                  </Button>
                                );
                              })}
                              <Button size="sm" variant="ghost" onClick={() => setTarget({ account, mode: activeTab.modes[0] })}>
                                <FlaskConical />{t("managedAccounts.test")}
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

      {target ? (
        <AccountTestDialog
          key={`${target.account.exportKey}:${target.mode}`}
          account={target.account}
          initialMode={target.mode}
          models={modelsQuery.data ?? []}
          onClose={() => setTarget(null)}
          onCompleted={() => {
            void accountsQuery.refetch();
          }}
        />
      ) : null}
    </div>
  );
}
