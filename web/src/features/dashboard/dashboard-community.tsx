import { ExternalLink, Star } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import { CopyButton } from "@/shared/components/copy-button";

const QQ_GROUP_NUMBER = "972295238";
const UPSTREAM_PROJECT_URL = "https://github.com/chenyme/grok2api";
const CUSTOM_PROJECT_URL = "https://github.com/LXXYSLF/grok-account-manager-main";

const STORE_PRODUCTS = [
  { name: "community.products.grokBuild.name", detail: "community.products.grokBuild.detail", price: "community.products.grokBuild.price" },
  { name: "community.products.gptFree.name", detail: "community.products.gptFree.detail", price: "community.products.gptFree.price" },
  { name: "community.products.codeBuddy.name", detail: "community.products.codeBuddy.detail", price: "community.products.codeBuddy.price" },
] as const;

export function DashboardCommunity() {
  const { t } = useTranslation();

  return (
    <DashboardPanel id="community" title={t("community.title")}>
      <p className="max-w-3xl text-xs leading-5 text-muted-foreground">
        {t("community.attribution")} {" "}
        <a className="text-foreground underline underline-offset-2 hover:no-underline" href={UPSTREAM_PROJECT_URL} target="_blank" rel="noreferrer">
          {t("community.attributionSource")}
        </a>
        {" · "}
        <a className="text-foreground underline underline-offset-2 hover:no-underline" href={CUSTOM_PROJECT_URL} target="_blank" rel="noreferrer">
          {t("community.attributionCustom")}
        </a>
      </p>
      <div className="mt-5 border-t border-border/70 pt-4">
        <div className="flex min-h-20 items-center justify-between gap-4 border-b border-border/70 pb-4">
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">{t("community.qqGroup")}</p>
            <p className="mt-1.5 text-lg font-semibold tracking-tight">{QQ_GROUP_NUMBER}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("community.groupHelp")}</p>
          </div>
          <CopyButton value={QQ_GROUP_NUMBER} copyLabel={t("community.copyGroupNumber")} className="size-9 shrink-0" />
        </div>
        <div className="pt-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">{t("community.productsTitle")}</p>
          <div className="mt-3 grid gap-y-4 md:grid-cols-3 md:gap-y-0">
            {STORE_PRODUCTS.map((product, index) => (
              <div
                key={product.name}
                className={index > 0 ? "min-w-0 border-t border-border/70 pt-4 md:border-l md:border-t-0 md:pl-5 md:pt-0" : "min-w-0"}
              >
                <p className="text-sm font-semibold">{t(product.name)}</p>
                <p className="mt-1 text-xs text-muted-foreground">{t(product.detail)}</p>
                <p className="mt-3 text-base font-semibold tabular-nums">{t(product.price)}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border/70 pt-4">
        <div className="min-w-0">
          <p className="text-sm font-medium">{t("community.starTitle")}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t("community.starHelp")}</p>
        </div>
        <Button asChild variant="secondary" size="sm" className="shrink-0">
          <a href={CUSTOM_PROJECT_URL} target="_blank" rel="noreferrer">
            <Star className="size-3.5" />
            {t("community.starAction")}
            <ExternalLink className="size-3.5" />
          </a>
        </Button>
      </div>
    </DashboardPanel>
  );
}
