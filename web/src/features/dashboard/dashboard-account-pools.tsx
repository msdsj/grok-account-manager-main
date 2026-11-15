import { Bot, ImageIcon, Sparkles, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import type { DashboardAccountPoolDTO, DashboardDTO } from "@/features/dashboard/dashboard-api";
import { cn } from "@/shared/lib/cn";
import { formatNumber } from "@/shared/lib/format";

const PROVIDERS: Array<{ key: string; labelKey: string; icon: LucideIcon }> = [
  { key: "grok", labelKey: "managedAccounts.tabs.grok", icon: Sparkles },
  { key: "kiro", labelKey: "managedAccounts.tabs.kiro", icon: Bot },
  { key: "chatgpt", labelKey: "managedAccounts.tabs.gpt", icon: ImageIcon },
];

type Props = {
  dashboard?: DashboardDTO;
  locale: string;
  loading: boolean;
};

export function DashboardAccountPools({ dashboard, locale, loading }: Props) {
  const { t } = useTranslation();
  const pools = dashboard?.accountPools ?? [];

  return (
    <section aria-label={t("dashboard.accountPoolsTitle")}>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h2 className="text-[13px] font-semibold">{t("dashboard.accountPoolsTitle")}</h2>
        <span className="text-[11px] text-muted-foreground">{t("dashboard.accountPoolsHint")}</span>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        {PROVIDERS.map((provider) => {
          const pool = pools.find((item) => item.provider === provider.key);
          return <PoolCard key={provider.key} provider={provider} pool={pool} locale={locale} loading={loading} />;
        })}
      </div>
    </section>
  );
}

function PoolCard({
  provider,
  pool,
  locale,
  loading,
}: {
  provider: { key: string; labelKey: string; icon: LucideIcon };
  pool?: DashboardAccountPoolDTO;
  locale: string;
  loading: boolean;
}) {
  const { t } = useTranslation();
  const Icon = provider.icon;
  const total = pool?.total ?? 0;
  const active = pool?.active ?? 0;
  const weeklyUsed = pool?.weeklyUsed ?? 0;
  const weeklyTotal = pool?.weeklyTotal ?? 0;
  const quotaPercent = weeklyTotal > 0 ? Math.min(100, Math.round((weeklyUsed / weeklyTotal) * 100)) : 0;
  const jobStatus = pool?.running ? "running" : pool?.lastJobStatus ?? "";
  const statusLabel = jobStatus ? t(`dashboard.jobStatus.${jobStatus}`, { defaultValue: jobStatus }) : t("dashboard.jobStatus.idle");

  return (
    <article className="rounded-xl border border-border/80 bg-card p-4 shadow-[var(--shadow-card)]" aria-busy={loading}>
      <header className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex size-7 items-center justify-center rounded-lg bg-secondary text-muted-foreground">
            <Icon className="size-4" strokeWidth={1.8} />
          </span>
          <span className="truncate text-[13px] font-semibold">{t(provider.labelKey)}</span>
        </div>
        <Badge variant={pool?.running ? "default" : jobStatus === "completed_with_errors" ? "destructive" : "outline"}>
          {statusLabel}
        </Badge>
      </header>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.08em] text-muted-foreground">{t("dashboard.poolAccounts")}</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">{loading ? <Spinner className="size-4" /> : formatNumber(total, locale)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-[0.08em] text-muted-foreground">{t("dashboard.poolActive")}</p>
          <p className="mt-1 text-xl font-semibold tabular-nums">{loading ? <Spinner className="size-4" /> : formatNumber(active, locale)}</p>
        </div>
      </div>

      <div className="mt-4 space-y-1.5">
        <div className="flex items-center justify-between text-[11px] text-muted-foreground">
          <span>{t("dashboard.poolWeeklyQuota")}</span>
          <span className="tabular-nums">
            {weeklyTotal > 0
              ? `${formatNumber(weeklyUsed, locale)} / ${formatNumber(weeklyTotal, locale)}`
              : t("managedAccounts.quotaNone")}
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-secondary">
          <div
            className={cn("h-full rounded-full transition-[width] duration-500", quotaPercent >= 90 ? "bg-destructive" : "bg-primary")}
            style={{ width: `${quotaPercent}%` }}
          />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-1.5 text-[11px] text-muted-foreground">
        <span className="rounded-md border border-border/60 px-1.5 py-0.5">{t("dashboard.poolChat", { count: formatNumber(pool?.chatAvailable ?? 0, locale) })}</span>
        <span className="rounded-md border border-border/60 px-1.5 py-0.5">{t("dashboard.poolImage", { count: formatNumber(pool?.imageAvailable ?? 0, locale) })}</span>
        <span className="rounded-md border border-border/60 px-1.5 py-0.5">{t("dashboard.poolVideo", { count: formatNumber(pool?.videoAvailable ?? 0, locale) })}</span>
      </div>
    </article>
  );
}
