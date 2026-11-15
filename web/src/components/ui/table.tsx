import * as React from "react"

import { cn } from "@/shared/lib/cn"

type TableProps = React.HTMLAttributes<HTMLTableElement> & {
  viewportRows?: number
  rowHeight?: number
}

const Table = React.forwardRef<HTMLTableElement, TableProps>(({ className, viewportRows, rowHeight, ...props }, ref) => (
  <div
    data-slot="table-scroll-container"
    className={cn(
      "relative w-full overflow-auto",
      viewportRows && "[&_thead]:sticky [&_thead]:top-0 [&_thead]:z-30 [&_thead]:bg-card"
    )}
    style={viewportRows && rowHeight ? { maxHeight: 36 + viewportRows * rowHeight } : undefined}
  >
    <table
      ref={ref}
      className={cn("w-full caption-bottom text-[13px]", className)}
      {...props}
    />
  </div>
))
Table.displayName = "Table"

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead ref={ref} className={cn("[&_tr]:border-b [&_tr]:border-border/70", className)} {...props} />
))
TableHeader.displayName = "TableHeader"

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody
    ref={ref}
    className={cn("[&_tr:last-child]:border-0", className)}
    {...props}
  />
))
TableBody.displayName = "TableBody"

const TableFooter = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tfoot
    ref={ref}
    className={cn(
      "border-t border-border/70 bg-muted/40 font-medium [&>tr]:last:border-b-0",
      className
    )}
    {...props}
  />
))
TableFooter.displayName = "TableFooter"

const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <tr
    ref={ref}
    className={cn(
      "border-b border-border/60 transition-colors hover:bg-[var(--row-hover)] data-[state=selected]:bg-[color-mix(in_oklab,var(--primary)_8%,var(--card))]",
      className
    )}
    {...props}
  />
))
TableRow.displayName = "TableRow"

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <th
    ref={ref}
    className={cn(
      "h-9 px-3 text-left align-middle text-[11px] font-medium uppercase tracking-[0.06em] text-muted-foreground [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[1px]",
      className
    )}
    {...props}
  />
))
TableHead.displayName = "TableHead"

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <td
    ref={ref}
    className={cn(
      "px-3 py-2.5 align-middle [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[1px]",
      className
    )}
    {...props}
  />
))
TableCell.displayName = "TableCell"

const TableActionHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <TableHead
    ref={ref}
    className={cn("sticky right-0 z-20 w-12 min-w-12 bg-card px-2", className)}
    {...props}
  />
))
TableActionHead.displayName = "TableActionHead"

const TableActionCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => (
  <TableCell
    ref={ref}
    className={cn(
      "sticky right-0 z-10 w-12 min-w-12 bg-card px-2 transition-colors group-hover:bg-[var(--row-hover)] group-data-[state=selected]:bg-[color-mix(in_oklab,var(--primary)_8%,var(--card))]",
      className
    )}
    {...props}
  />
))
TableActionCell.displayName = "TableActionCell"

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(({ className, ...props }, ref) => (
  <caption
    ref={ref}
    className={cn("mt-4 text-xs text-muted-foreground", className)}
    {...props}
  />
))
TableCaption.displayName = "TableCaption"

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableActionHead,
  TableActionCell,
  TableCaption,
}
