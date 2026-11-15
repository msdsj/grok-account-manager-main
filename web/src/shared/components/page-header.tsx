import type { ReactNode } from "react";

import { cn } from "@/shared/lib/cn";

export function PageHeader({ title, description, actions, className }: { title: string; description: string; actions?: ReactNode; className?: string }) {
  return (
    <header className={cn("flex shrink-0 flex-col gap-3 sm:flex-row sm:items-end sm:justify-between", className)}>
      <div className="min-w-0">
        <h1 className="truncate text-[20px] font-semibold leading-7 tracking-[-0.022em] text-foreground">{title}</h1>
        <p className="mt-1 max-w-3xl text-[12.5px] leading-5 text-muted-foreground">{description}</p>
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
