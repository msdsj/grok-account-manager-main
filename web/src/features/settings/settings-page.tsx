import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, Moon, RefreshCw, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import {
  getMailConfig,
  getRegistrationProxyPool,
  saveMailConfig,
  type EmailSource,
} from "@/features/register-task/register-task-api";
import { getGatewayStatus } from "@/shared/api/gateway";
import { PageHeader } from "@/shared/components/page-header";

const emailSources: Array<{ value: EmailSource; label: string }> = [
  { value: "duckmail", label: "DuckMail" },
  { value: "cloud_mail", label: "Cloud Mail" },
  { value: "outlook", label: "Outlook" },
  { value: "google", label: "Google 账号" },
];

export function SettingsPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const { theme, setTheme } = useTheme();
  const [emailSource, setEmailSource] = useState<EmailSource>("duckmail");
  const [duckMailApiBase, setDuckMailApiBase] = useState("");
  const [duckMailDomain, setDuckMailDomain] = useState("");
  const [duckMailApiKey, setDuckMailApiKey] = useState("");
  const [cloudMailApiBase, setCloudMailApiBase] = useState("");
  const [cloudMailDomains, setCloudMailDomains] = useState("");
  const [cloudMailAuthMode, setCloudMailAuthMode] = useState<"public" | "login">("login");
  const [cloudMailPublicToken, setCloudMailPublicToken] = useState("");
  const [cloudMailLoginEmail, setCloudMailLoginEmail] = useState("");
  const [cloudMailLoginPassword, setCloudMailLoginPassword] = useState("");
  const [duckMailApiKeyConfigured, setDuckMailApiKeyConfigured] = useState(false);
  const [cloudMailPublicTokenConfigured, setCloudMailPublicTokenConfigured] = useState(false);
  const [cloudMailLoginPasswordConfigured, setCloudMailLoginPasswordConfigured] = useState(false);
  const loadedMailConfig = useRef(false);

  const mailQuery = useQuery({ queryKey: ["settings", "mail-config"], queryFn: getMailConfig });
  const statusQuery = useQuery({ queryKey: ["gateway-status"], queryFn: () => getGatewayStatus(), staleTime: 30_000 });
  const proxiesQuery = useQuery({ queryKey: ["registration-proxy-pool"], queryFn: getRegistrationProxyPool, staleTime: 30_000 });

  useEffect(() => {
    const config = mailQuery.data;
    if (!config || loadedMailConfig.current) return;
    if (emailSources.some((item) => item.value === config.emailSource)) setEmailSource(config.emailSource);
    setDuckMailApiBase(config.duckMailApiBase);
    setDuckMailDomain(config.duckMailDomain);
    setDuckMailApiKeyConfigured(config.duckMailApiKeyConfigured);
    setCloudMailApiBase(config.cloudMailApiBase);
    setCloudMailDomains(config.cloudMailDomains);
    setCloudMailAuthMode(config.cloudMailAuthMode);
    setCloudMailPublicTokenConfigured(config.cloudMailPublicTokenConfigured);
    setCloudMailLoginEmail(config.cloudMailLoginEmail);
    setCloudMailLoginPasswordConfigured(config.cloudMailLoginPasswordConfigured);
    loadedMailConfig.current = true;
  }, [mailQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () => saveMailConfig({
      emailSource,
      duckMailApiBase,
      duckMailApiKey: duckMailApiKey.trim() || undefined,
      duckMailDomain,
      cloudMailApiBase,
      cloudMailDomains,
      cloudMailAuthMode,
      cloudMailPublicToken: cloudMailPublicToken.trim() || undefined,
      cloudMailLoginEmail,
      cloudMailLoginPassword: cloudMailLoginPassword.trim() || undefined,
    }),
    onSuccess: (config) => {
      queryClient.setQueryData(["settings", "mail-config"], config);
      setDuckMailApiKeyConfigured(config.duckMailApiKeyConfigured);
      setCloudMailPublicTokenConfigured(config.cloudMailPublicTokenConfigured);
      setCloudMailLoginPasswordConfigured(config.cloudMailLoginPasswordConfigured);
      setDuckMailApiKey("");
      setCloudMailPublicToken("");
      setCloudMailLoginPassword("");
      toast.success(t("settings.saved"));
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : t("errors.generic")),
  });

  const providers = statusQuery.data?.providers ?? [];
  const themeItems = [
    { value: "light", label: t("shell.light"), icon: Sun },
    { value: "dark", label: t("shell.dark"), icon: Moon },
    { value: "system", label: t("shell.system"), icon: Monitor },
  ] as const;

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("settings.title")}
        description="集中管理邮箱配置、网关状态与界面主题。"
        actions={(
          <Button variant="secondary" size="sm" onClick={() => void mailQuery.refetch()} disabled={mailQuery.isFetching}>
            <RefreshCw className={mailQuery.isFetching ? "animate-spin" : undefined} />{t("common.refresh")}
          </Button>
        )}
      />

      {mailQuery.isError ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          邮箱配置加载失败：{mailQuery.error instanceof Error ? mailQuery.error.message : t("errors.generic")}
        </div>
      ) : null}

      <form onSubmit={(event) => { event.preventDefault(); saveMutation.mutate(); }}>
        <DashboardPanel
          id="settings-mail-config"
          title="邮箱配置"
          actions={(
            <Button type="submit" size="sm" disabled={mailQuery.isPending || saveMutation.isPending}>
              {saveMutation.isPending ? <Spinner /> : null}{t("common.save")}
            </Button>
          )}
          contentClassName="space-y-4"
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="settings-email-source">默认邮箱来源</Label>
              <Select value={emailSource} onValueChange={(value) => setEmailSource(value as EmailSource)}>
                <SelectTrigger id="settings-email-source"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {emailSources.map((source) => <SelectItem key={source.value} value={source.value}>{source.label}</SelectItem>)}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">Outlook / Google 账号池在注册机页面维护。</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="settings-duck-domain">DuckMail 邮箱域名</Label>
              <Input id="settings-duck-domain" value={duckMailDomain} onChange={(event) => setDuckMailDomain(event.target.value)} placeholder="@example.com" />
            </div>
          </div>

          <div className="space-y-3 rounded-lg border border-border/70 p-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">DuckMail</h3>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="settings-duck-base">DuckMail 地址</Label>
                <Input id="settings-duck-base" value={duckMailApiBase} onChange={(event) => setDuckMailApiBase(event.target.value)} placeholder="https://api.duckmail.example" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="settings-duck-key">DuckMail API Key</Label>
                <Input
                  id="settings-duck-key"
                  type="password"
                  autoComplete="off"
                  value={duckMailApiKey}
                  onChange={(event) => setDuckMailApiKey(event.target.value)}
                  placeholder={duckMailApiKeyConfigured ? "已配置，留空保持不变" : "输入 API Key"}
                />
              </div>
            </div>
          </div>

          <div className="space-y-3 rounded-lg border border-border/70 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">Cloud Mail</h3>
              <Tabs value={cloudMailAuthMode} onValueChange={(value) => setCloudMailAuthMode(value as "public" | "login")}>
                <TabsList>
                  <TabsTrigger value="public">Public Token</TabsTrigger>
                  <TabsTrigger value="login">账号登录</TabsTrigger>
                </TabsList>
              </Tabs>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="settings-cloud-base">Cloud Mail 地址</Label>
                <Input id="settings-cloud-base" value={cloudMailApiBase} onChange={(event) => setCloudMailApiBase(event.target.value)} placeholder="https://mail.example.com" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="settings-cloud-domains">邮箱域名</Label>
                <Input id="settings-cloud-domains" value={cloudMailDomains} onChange={(event) => setCloudMailDomains(event.target.value)} placeholder="example.com，多个域名用逗号分隔" />
              </div>
              {cloudMailAuthMode === "public" ? (
                <div className="space-y-1.5 sm:col-span-2">
                  <Label htmlFor="settings-cloud-token">Public Token</Label>
                  <Input
                    id="settings-cloud-token"
                    type="password"
                    autoComplete="off"
                    value={cloudMailPublicToken}
                    onChange={(event) => setCloudMailPublicToken(event.target.value)}
                    placeholder={cloudMailPublicTokenConfigured ? "已配置，留空保持不变" : "输入 Public Token"}
                  />
                </div>
              ) : (
                <>
                  <div className="space-y-1.5">
                    <Label htmlFor="settings-cloud-email">登录邮箱</Label>
                    <Input id="settings-cloud-email" value={cloudMailLoginEmail} onChange={(event) => setCloudMailLoginEmail(event.target.value)} placeholder="admin@example.com" />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="settings-cloud-password">登录密码</Label>
                    <Input
                      id="settings-cloud-password"
                      type="password"
                      autoComplete="off"
                      value={cloudMailLoginPassword}
                      onChange={(event) => setCloudMailLoginPassword(event.target.value)}
                      placeholder={cloudMailLoginPasswordConfigured ? "已配置，留空保持不变" : "输入登录密码"}
                    />
                  </div>
                </>
              )}
            </div>
          </div>
        </DashboardPanel>
      </form>

      <DashboardPanel
        id="settings-gateway-status"
        title="网关状态"
        actions={(
          <Button variant="ghost" size="sm" onClick={() => void statusQuery.refetch()} disabled={statusQuery.isFetching}>
            <RefreshCw className={statusQuery.isFetching ? "animate-spin" : undefined} />{t("common.refresh")}
          </Button>
        )}
      >
        {statusQuery.isPending ? (
          <div className="flex justify-center py-8"><Spinner className="size-5" /></div>
        ) : statusQuery.isError ? (
          <p className="py-6 text-center text-sm text-destructive">网关状态加载失败</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {providers.map((provider) => (
              <div key={provider.provider} className="rounded-lg border border-border/70 bg-secondary/25 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium">{provider.provider}</span>
                  <Badge variant="outline">{provider.accounts} 账号</Badge>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{provider.models} 个可用模型</p>
              </div>
            ))}
            {providers.length === 0 ? <p className="py-6 text-center text-sm text-muted-foreground sm:col-span-2 xl:col-span-3">{t("common.noData")}</p> : null}
          </div>
        )}
      </DashboardPanel>

      <div className="grid gap-5 lg:grid-cols-2">
        <DashboardPanel id="settings-theme" title="主题设置">
          <p className="text-xs text-muted-foreground">当前主题：{themeItems.find((item) => item.value === (theme ?? "system"))?.label ?? t("shell.system")}</p>
          <Tabs value={theme ?? "system"} onValueChange={setTheme} className="mt-3">
            <TabsList className="w-full">
              {themeItems.map(({ value, label, icon: Icon }) => (
                <TabsTrigger key={value} value={value} className="flex-1 gap-1.5"><Icon className="size-3.5" />{label}</TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </DashboardPanel>

        <DashboardPanel id="settings-proxies" title="注册代理池">
          {proxiesQuery.isPending ? (
            <div className="flex justify-center py-6"><Spinner className="size-5" /></div>
          ) : proxiesQuery.isError ? (
            <p className="py-4 text-sm text-destructive">代理池加载失败</p>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3 rounded-lg border border-border/70 bg-secondary/25 px-4 py-3">
                <span className="text-sm">已保存代理</span>
                <span className="text-lg font-semibold tabular-nums">{proxiesQuery.data?.count ?? 0}</span>
              </div>
              <p className="text-xs text-muted-foreground">代理导入与清理请前往注册机页面。</p>
              <Button variant="secondary" size="sm" asChild><Link to="/register">前往注册机</Link></Button>
            </div>
          )}
        </DashboardPanel>
      </div>

      <p className="px-1 text-xs text-muted-foreground">
        数据存储于本地 Python 后端；当前语言：{i18n.language === "zh-CN" ? "简体中文" : "English"}。
      </p>
    </div>
  );
}
