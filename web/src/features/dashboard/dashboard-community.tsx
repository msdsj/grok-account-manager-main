import { ExternalLink, Star } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DashboardPanel } from "@/features/dashboard/dashboard-panel";
import { CopyButton } from "@/shared/components/copy-button";

const QQ_GROUP_NUMBER = "972295238";
const STORE_URL = "https://pay.ldxp.cn/item/gecsrk";
const UPSTREAM_PROJECT_URL = "https://github.com/chenyme/grok2api";
const CUSTOM_PROJECT_URL = "https://github.com/LXXYSLF/grok-account-manager-main";
const DONATION_IMAGE_URL = "/sponner/thank-you-qr.jpg";

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
      <div className="mt-5 grid gap-4 border-t border-border/70 pt-4 sm:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_300px]">
        <div className="flex min-h-20 items-center justify-between gap-4 border-b border-border/70 pb-4 sm:border-b-0 sm:border-r sm:pb-0 sm:pr-5">
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">{t("community.qqGroup")}</p>
            <p className="mt-1.5 text-lg font-semibold tracking-tight">{QQ_GROUP_NUMBER}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("community.groupHelp")}</p>
          </div>
          <CopyButton value={QQ_GROUP_NUMBER} copyLabel={t("community.copyGroupNumber")} className="size-9 shrink-0" />
        </div>
        <a
          className="group flex min-h-20 min-w-0 items-center justify-between gap-4 transition-colors hover:text-foreground"
          href={STORE_URL}
          target="_blank"
          rel="noreferrer"
        >
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">{t("community.storeLink")}</p>
            <p className="mt-1.5 text-lg font-semibold tracking-tight">{t("community.storeTitle")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("community.storeHelp")}</p>
          </div>
          <ExternalLink className="size-5 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
        </a>
        <div className="flex min-w-0 flex-col gap-3 border-t border-border/70 pt-4 sm:col-span-2 xl:col-span-1 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0">
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">{t("community.donationAction")}</p>
            <p className="mt-1.5 text-lg font-semibold tracking-tight">{t("community.donationTitle")}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("community.donationDescription")}</p>
          </div>
          <img
            src={DONATION_IMAGE_URL}
            alt={t("community.donationImageAlt")}
            className="h-auto w-full max-w-[280px] rounded-md border bg-white object-contain"
          />
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
