import type { ReactNode } from "react";

import { cn } from "@/shared/lib/cn";

type DataTableShellProps = {
  toolbar: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
};

export function DataTableShell({ toolbar, children, footer, className }: DataTableShellProps) {
  return (
    <section className={cn("flex min-w-0 flex-col gap-3", className)}>
      <div className="flex min-h-10 flex-wrap items-center justify-between gap-3">
        {toolbar}
      </div>
      <div className="min-w-0 overflow-hidden rounded-xl border border-border/80 bg-card shadow-[var(--shadow-card)]">
        {children}
      </div>
      {footer ? (
        <div className="flex min-h-10 items-center">
          {footer}
        </div>
      ) : null}
    </section>
  );
}
