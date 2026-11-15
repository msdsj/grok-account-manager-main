import type { ReactNode } from "react";

import { cn } from "@/shared/lib/cn";

type DashboardPanelProps = {
  id: string;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
};

export function DashboardPanel({ id, title, actions, children, className, contentClassName }: DashboardPanelProps) {
  return (
    <section
      className={cn(
        "rounded-xl border border-border/80 bg-card p-4 shadow-[var(--shadow-card)] sm:p-5",
        className,
      )}
      aria-labelledby={id}
    >
      <header className="flex min-h-6 items-center justify-between gap-3">
        <h2 id={id} className="text-[13px] font-semibold tracking-[-0.01em]">{title}</h2>
        {actions ? <div className="shrink-0">{actions}</div> : null}
      </header>
      <div className={cn("mt-4", contentClassName)}>{children}</div>
    </section>
  );
}
