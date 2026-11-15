import { zodResolver } from "@hookform/resolvers/zod";
import {
  BookOpen,
  Box,
  ChevronDown,
  ChevronRight,
  Eye,
  ExternalLink,
  HeartHandshake,
  Image,
  KeyRound,
  Languages,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquareText,
  Monitor,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  ScrollText,
  Settings,
  Sparkles,
  Sun,
  Tags,
  Video,
  type LucideIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState, type ReactNode } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { toast } from "sonner";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useAuth } from "@/shared/auth/use-auth";
import { GitHubMark } from "@/shared/components/github-mark";
import { SiteFooter } from "@/shared/components/site-footer";
import { cn } from "@/shared/lib/cn";

type NavItem = { href: string; label: string; title: string; description: string; icon: LucideIcon };
type NavGroup = { key: string; label: string; items: NavItem[] };

const navigationGroups: NavGroup[] = [
  {
    key: "overview",
    label: "navGroups.overview",
    items: [
      { href: "/dashboard", label: "nav.dashboard", title: "dashboard.title", description: "dashboard.description", icon: LayoutDashboard },
    ],
  },
  {
    key: "resources",
    label: "navGroups.resources",
    items: [
      { href: "/managed-accounts", label: "nav.managedAccounts", title: "managedAccounts.title", description: "managedAccounts.description", icon: Tags },
      { href: "/models", label: "nav.models", title: "models.title", description: "models.description", icon: Box },
    ],
  },
  {
    key: "production",
    label: "navGroups.production",
    items: [
      { href: "/register", label: "nav.registerTask", title: "registerTask.title", description: "registerTask.description", icon: Play },
      { href: "/creative-console", label: "nav.creativeConsole", title: "creativeConsole.title", description: "creativeConsole.description", icon: Sparkles },
    ],
  },
  {
    key: "gateway",
    label: "navGroups.gateway",
    items: [
      { href: "/client-keys", label: "nav.clientKeys", title: "keys.title", description: "keys.description", icon: KeyRound },
      { href: "/request-audits", label: "nav.audits", title: "audits.title", description: "audits.description", icon: Eye },
    ],
  },
  {
    key: "system",
    label: "navGroups.system",
    items: [
      { href: "/operation-logs", label: "nav.operationLogs", title: "operationLogs.title", description: "operationLogs.description", icon: ScrollText },
      { href: "/settings", label: "nav.settings", title: "settings.title", description: "settings.description", icon: Settings },
    ],
  },
];

const docsGroup: NavGroup = {
  key: "access",
  label: "navGroups.access",
  items: [
    { href: "/docs/chat/completions", label: "nav.docs", title: "docs.title", description: "docs.description", icon: BookOpen },
  ],
};

const documentation = [
  {
    label: "Chat",
    icon: MessageSquareText,
    items: [
      { href: "/docs/chat/completions", label: "Chat Completions", method: "POST" },
      { href: "/docs/chat/responses", label: "Responses", method: "POST" },
      { href: "/docs/chat/messages", label: "Messages", method: "POST" },
    ],
  },
  {
    label: "Image",
    icon: Image,
    items: [
      { href: "/docs/image/generations", label: "Image Generations", method: "POST" },
      { href: "/docs/image/edits", label: "Image Edits", method: "POST" },
    ],
  },
  {
    label: "Video",
    icon: Video,
    items: [
      { href: "/docs/video/generations", label: "Video Generations", method: "POST" },
      { href: "/docs/video/get", label: "Get Video", method: "GET" },
    ],
  },
] as const;

const ATTRIBUTION_DISMISSED_STORAGE_KEY = "grok2api:attribution-dismissed";
const SIDEBAR_COLLAPSED_STORAGE_KEY = "grok2api:sidebar-collapsed";

function attributionWasDismissed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(ATTRIBUTION_DISMISSED_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function sidebarWasCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function AppShell() {
  const { t, i18n } = useTranslation();
  const { admin, logout, changePassword } = useAuth();
  const location = useLocation();
  const { theme, setTheme } = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(sidebarWasCollapsed);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [attributionOpen, setAttributionOpen] = useState(() => !attributionWasDismissed());
  const [documentationOpen, setDocumentationOpen] = useState<Record<string, boolean>>({});
  const isMediaWorkspace = location.pathname === "/creative-console";
  const isFixedWorkspace = location.pathname === "/creative-console";

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, collapsed ? "1" : "0");
    } catch {
      // Storage can be unavailable in private browsing; collapse still works in-memory.
    }
  }, [collapsed]);

  const passwordSchema = z.object({
    currentPassword: z.string().min(1, t("errors.required")),
    newPassword: z.string().min(8, t("errors.minPassword")),
  });
  type PasswordForm = z.infer<typeof passwordSchema>;
  const passwordForm = useForm<PasswordForm>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { currentPassword: "", newPassword: "" },
  });

  async function submitPassword(values: PasswordForm): Promise<void> {
    try {
      await changePassword(values.currentPassword, values.newPassword);
      toast.success(t("auth.passwordUpdated"));
      passwordForm.reset();
      setPasswordOpen(false);
      await logout();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("errors.generic"));
    }
  }

  function closeAttribution(): void {
    setAttributionOpen(false);
    try {
      window.localStorage.setItem(ATTRIBUTION_DISMISSED_STORAGE_KEY, "1");
    } catch {
      // Private browsing or disabled storage should not prevent closing the dialog.
    }
  }

  const allGroups = [...navigationGroups.slice(0, 4), docsGroup, ...navigationGroups.slice(4)];
  const activeItem = allGroups.flatMap((group) => group.items).find((item) => location.pathname === item.href || location.pathname.startsWith(`${item.href}/`))
    ?? (location.pathname.startsWith("/docs") ? docsGroup.items[0] : undefined);
  const activeGroup = allGroups.find((group) => group.items.some((item) => item === activeItem));
  const pageTitle = activeItem ? t(activeItem.title) : t("appName");
  const pageDescription = activeItem ? t(activeItem.description) : "";
  const groupLabel = activeGroup ? t(activeGroup.label) : "";

  function navItem({ item, isCollapsed, onNavigate }: { item: NavItem; isCollapsed: boolean; onNavigate?: () => void }): ReactNode {
    const Icon = item.icon;
    return (
      <NavLink
        key={item.href}
        to={item.href}
        onClick={onNavigate}
        title={isCollapsed ? t(item.label) : undefined}
        className={({ isActive }) => cn(
          "group/nav relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-[13px] font-normal text-sidebar-foreground/72 transition-colors duration-150 hover:bg-sidebar-accent hover:text-sidebar-foreground",
          isActive && "bg-[color-mix(in_oklab,var(--primary)_13%,transparent)] font-medium text-primary hover:bg-[color-mix(in_oklab,var(--primary)_16%,transparent)] hover:text-primary",
          isCollapsed && "justify-center px-0",
        )}
      >
        {({ isActive }) => (
          <>
            {isActive ? <span aria-hidden="true" className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-primary" /> : null}
            <Icon className={cn("size-4 shrink-0", isActive ? "text-primary" : "text-sidebar-foreground/55 transition-colors group-hover/nav:text-sidebar-foreground")} strokeWidth={1.8} />
            {!isCollapsed ? <span className="min-w-0 flex-1 truncate">{t(item.label)}</span> : null}
          </>
        )}
      </NavLink>
    );
  }

  function documentationLinks(isCollapsed: boolean): ReactNode {
    if (isCollapsed) {
      return (
        <NavLink
          to="/docs/chat/completions"
          title={t("nav.docs")}
          className={({ isActive }) => cn(
            "relative flex h-8 items-center justify-center rounded-md text-sidebar-foreground/72 transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground",
            isActive && "bg-[color-mix(in_oklab,var(--primary)_13%,transparent)] text-primary",
          )}
        >
          {({ isActive }) => (
            <>
              {isActive ? <span aria-hidden="true" className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-primary" /> : null}
              <BookOpen className="size-4" strokeWidth={1.8} />
            </>
          )}
        </NavLink>
      );
    }

    return (
      <>
        <NavLink
          to="/docs/chat/completions"
          onClick={() => setMobileOpen(false)}
          className={({ isActive }) => cn(
            "group/nav relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-[13px] text-sidebar-foreground/72 transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground",
            isActive && "font-medium text-primary",
          )}
        >
          <BookOpen className="size-4 shrink-0 text-sidebar-foreground/55" strokeWidth={1.8} />
          <span className="min-w-0 flex-1 truncate">{t("nav.docs")}</span>
        </NavLink>
        <div className="space-y-0.5 pb-1 pl-3">
          {documentation.map(({ label, icon: Icon, items }) => {
            const open = documentationOpen[label] ?? false;
            return (
              <div key={label}>
                <button
                  type="button"
                  className="flex h-7 w-full items-center gap-2 rounded-md px-2 text-[11px] font-medium uppercase tracking-[0.08em] text-sidebar-foreground/45 transition-colors hover:text-sidebar-foreground/75"
                  aria-expanded={open}
                  onClick={() => setDocumentationOpen((current) => ({ ...current, [label]: !open }))}
                >
                  <Icon className="size-3 shrink-0" strokeWidth={1.7} />
                  <span className="min-w-0 flex-1 text-left">{label}</span>
                  <ChevronDown className={cn("size-3 transition-transform", !open && "-rotate-90")} />
                </button>
                <div className={cn(
                  "grid transition-[grid-template-rows,opacity] duration-200 ease-out",
                  open ? "grid-rows-[1fr] opacity-100" : "pointer-events-none grid-rows-[0fr] opacity-0",
                )} aria-hidden={!open}>
                  <div className="overflow-hidden">
                    <div className="space-y-0.5 pt-0.5">
                      {items.map((item) => (
                        <NavLink
                          key={item.href}
                          to={item.href}
                          onClick={() => setMobileOpen(false)}
                          className={({ isActive }) => cn(
                            "flex h-7 min-w-0 items-center gap-2 rounded-md pl-6 pr-2 text-[11.5px] text-sidebar-foreground/60 transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground",
                            isActive && "bg-sidebar-accent text-sidebar-foreground",
                          )}
                        >
                          <span className="min-w-0 flex-1 truncate">{item.label}</span>
                          <span className={cn(
                            "shrink-0 font-mono text-[9px] font-medium",
                            item.method === "GET" ? "text-emerald-600 dark:text-emerald-400" : "text-sky-600 dark:text-sky-400",
                          )}>
                            {item.method}
                          </span>
                        </NavLink>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </>
    );
  }

  function navigation(isCollapsed: boolean, onNavigate?: () => void): ReactNode {
    return (
      <nav className={cn("flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto overscroll-contain px-2 pb-2", isCollapsed && "px-2")} aria-label={t("shell.navigation")}>
        {navigationGroups.slice(0, 4).map((group) => (
          <div key={group.key} className="flex flex-col gap-0.5">
            <div className={cn("px-2 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/38", isCollapsed && "mx-auto my-1 h-px w-6 bg-sidebar-border p-0 pt-0 text-transparent")}>
              {isCollapsed ? "" : t(group.label)}
            </div>
            {group.items.map((item) => navItem({ item, isCollapsed, onNavigate }))}
          </div>
        ))}

        <div className="flex flex-col gap-0.5">
          <div className={cn("px-2 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/38", isCollapsed && "mx-auto my-1 h-px w-6 bg-sidebar-border p-0 pt-0 text-transparent")}>
            {isCollapsed ? "" : t(docsGroup.label)}
          </div>
          {documentationLinks(isCollapsed)}
        </div>

        {navigationGroups.slice(4).map((group) => (
          <div key={group.key} className="flex flex-col gap-0.5">
            <div className={cn("px-2 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-sidebar-foreground/38", isCollapsed && "mx-auto my-1 h-px w-6 bg-sidebar-border p-0 pt-0 text-transparent")}>
              {isCollapsed ? "" : t(group.label)}
            </div>
            {group.items.map((item) => navItem({ item, isCollapsed, onNavigate }))}
          </div>
        ))}
      </nav>
    );
  }

  function accountControl(isCollapsed: boolean): ReactNode {
    const initial = (admin?.username ?? "A").slice(0, 1).toUpperCase();
    return (
      <div className={cn("flex h-10 items-center gap-2 border-t border-sidebar-border/70 px-2 pt-2", isCollapsed && "justify-center px-0")}>
        <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[linear-gradient(180deg,var(--primary),color-mix(in_oklab,var(--primary)_72%,#000))] text-[10px] font-semibold text-white shadow-sm">
          {initial}
        </span>
        {!isCollapsed ? <span className="min-w-0 flex-1 truncate text-xs text-sidebar-foreground/70">{admin?.username}</span> : null}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="size-7 shrink-0 text-sidebar-foreground/55 hover:bg-sidebar-accent hover:text-sidebar-foreground" aria-label={t("common.actions")}><MoreHorizontal /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="top" sideOffset={8} className="w-56 p-1.5">
            <DropdownMenuSub>
              <DropdownMenuSubTrigger className="h-8"><Sun />{t("shell.appearance")}</DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                <DropdownMenuItem onClick={() => setTheme("light")}><Sun />{t("shell.light")}</DropdownMenuItem>
                <DropdownMenuItem onClick={() => setTheme("dark")}><Moon />{t("shell.dark")}</DropdownMenuItem>
                <DropdownMenuItem onClick={() => setTheme("system")}><Monitor />{t("shell.system")}</DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuSub>
              <DropdownMenuSubTrigger className="h-8"><Languages />{t("shell.language")}</DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                <DropdownMenuItem onClick={() => void i18n.changeLanguage("zh-CN")}>简体中文</DropdownMenuItem>
                <DropdownMenuItem onClick={() => void i18n.changeLanguage("en")}>English</DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuItem className="h-8" onClick={() => setPasswordOpen(true)}><KeyRound />{t("auth.changePassword")}</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="h-8" onClick={() => void logout()}><LogOut />{t("auth.signOut")}</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    );
  }

  const themeOptions = [
    { value: "light", icon: Sun, label: t("shell.light") },
    { value: "dark", icon: Moon, label: t("shell.dark") },
    { value: "system", icon: Monitor, label: t("shell.system") },
  ] as const;

  return (
    <div className="app-canvas min-h-screen bg-background lg:flex lg:h-screen lg:items-center lg:justify-center lg:p-3">
      <div className="flex w-full min-w-0 flex-col lg:h-[calc(100vh-1.5rem)] lg:flex-row lg:overflow-hidden lg:rounded-2xl lg:border lg:border-border/70 lg:bg-card/45 lg:shadow-[var(--shadow-window)]">
        <aside
          className="mac-glass hidden shrink-0 flex-col border-r border-sidebar-border transition-[width] duration-200 ease-out lg:flex"
          style={{ width: collapsed ? 76 : 264 }}
        >
          <div className={cn("flex h-11 shrink-0 items-center gap-1.5 px-3", collapsed && "justify-center px-0")}>
            <span className="size-3 rounded-full border border-black/10 bg-[#ff5f57] shadow-[inset_0_-1px_1px_rgba(0,0,0,0.12)]" aria-hidden="true" />
            <span className="size-3 rounded-full border border-black/10 bg-[#febc2e] shadow-[inset_0_-1px_1px_rgba(0,0,0,0.12)]" aria-hidden="true" />
            <span className="size-3 rounded-full border border-black/10 bg-[#28c840] shadow-[inset_0_-1px_1px_rgba(0,0,0,0.12)]" aria-hidden="true" />
            {!collapsed ? (
              <Button
                variant="ghost"
                size="icon"
                className="ml-auto size-7 text-sidebar-foreground/45 hover:bg-sidebar-accent hover:text-sidebar-foreground"
                aria-label={t("shell.navigation")}
                onClick={() => setCollapsed(true)}
              >
                <PanelLeftClose className="size-3.5" />
              </Button>
            ) : null}
          </div>

          <div className={cn("relative flex h-12 shrink-0 items-center gap-2 px-4", collapsed && "justify-center px-0")}>
            <Link to="/dashboard" className="flex min-w-0 items-center gap-2.5">
              <span className="flex size-7 shrink-0 items-center justify-center rounded-[9px] bg-[linear-gradient(160deg,#0a84ff,#0055c4)] text-[13px] font-semibold text-white shadow-sm">G</span>
              {!collapsed ? (
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-semibold leading-4 tracking-tight text-sidebar-foreground">{t("appName")}</span>
                </span>
              ) : null}
            </Link>
          </div>
          {collapsed ? (
            <div className="flex justify-center pb-1 pt-0.5">
              <Button
                variant="ghost"
                size="icon"
                className="size-7 text-sidebar-foreground/45 hover:bg-sidebar-accent hover:text-sidebar-foreground"
                aria-label={t("shell.navigation")}
                onClick={() => setCollapsed(false)}
              >
                <PanelLeftOpen className="size-3.5" />
              </Button>
            </div>
          ) : null}

          {navigation(collapsed)}
          <div className="px-2 pb-2">{accountControl(collapsed)}</div>
        </aside>

        <div className="flex min-h-screen min-w-0 flex-1 flex-col lg:min-h-0 lg:overflow-hidden lg:rounded-r-2xl lg:bg-background">
          <header className="mac-toolbar sticky top-0 z-40 flex h-14 shrink-0 items-center justify-between gap-3 border-b border-border/60 px-3 sm:px-5">
            <div className="flex min-w-0 items-center gap-2.5">
              <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
                <SheetTrigger asChild>
                  <Button variant="ghost" size="icon" className="size-8 shrink-0 lg:hidden" aria-label={t("shell.openNavigation")}>
                    <Menu className="size-4" />
                  </Button>
                </SheetTrigger>
                <SheetContent side="left" className="mac-glass flex h-dvh max-h-dvh w-[278px] flex-col gap-0 overflow-hidden border-r border-sidebar-border px-0 py-0 [&>button]:right-2 [&>button]:top-3 [&>button]:flex [&>button]:size-7 [&>button]:items-center [&>button]:justify-center">
                  <SheetHeader className="sr-only">
                    <SheetTitle>{t("appName")}</SheetTitle>
                    <SheetDescription>{t("shell.navigation")}</SheetDescription>
                  </SheetHeader>
                  <div className="flex h-11 shrink-0 items-center gap-1.5 px-3">
                    <span className="size-3 rounded-full bg-[#ff5f57]" aria-hidden="true" />
                    <span className="size-3 rounded-full bg-[#febc2e]" aria-hidden="true" />
                    <span className="size-3 rounded-full bg-[#28c840]" aria-hidden="true" />
                  </div>
                  <div className="flex h-12 shrink-0 items-center gap-2.5 px-4">
                    <span className="flex size-7 items-center justify-center rounded-[9px] bg-[linear-gradient(160deg,#0a84ff,#0055c4)] text-[13px] font-semibold text-white shadow-sm">G</span>
                    <span className="min-w-0">
                      <span className="block truncate text-[13px] font-semibold leading-4 tracking-tight text-sidebar-foreground">{t("appName")}</span>
                    </span>
                  </div>
                  {navigation(false, () => setMobileOpen(false))}
                  <div className="px-2 pb-2">{accountControl(false)}</div>
                </SheetContent>
              </Sheet>

              <div className="flex min-w-0 items-center gap-2">
                <span className="hidden shrink-0 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground/80 sm:inline">
                  {groupLabel}
                </span>
                <ChevronRight className="hidden size-3 shrink-0 text-muted-foreground/40 sm:block" />
                <span className="truncate text-[13px] font-semibold text-foreground">{pageTitle}</span>
              </div>
              {pageDescription ? <span className="hidden min-w-0 truncate text-[11px] text-muted-foreground xl:inline">· {pageDescription}</span> : null}
            </div>

            <div className="flex shrink-0 items-center gap-1.5">
              <div className="flex items-center rounded-lg border border-border/70 bg-secondary/60 p-0.5">
                {themeOptions.map(({ value, icon: Icon, label }) => {
                  const active = (theme ?? "system") === value;
                  return (
                    <button
                      key={value}
                      type="button"
                      title={label}
                      aria-label={label}
                      aria-pressed={active}
                      onClick={() => setTheme(value)}
                      className={cn(
                        "flex size-6 items-center justify-center rounded-[6px] text-muted-foreground transition-colors hover:text-foreground",
                        active && "bg-[var(--segment-active)] text-foreground shadow-[0_1px_2px_rgba(0,0,0,0.1)]",
                      )}
                    >
                      <Icon className="size-3.5" strokeWidth={1.8} />
                    </button>
                  );
                })}
              </div>
              <Button variant="ghost" size="icon" className="hidden size-8 text-muted-foreground hover:text-foreground sm:inline-flex" asChild>
                <a href="https://github.com/LXXYSLF/grok-account-manager-main" target="_blank" rel="noreferrer" aria-label="GitHub">
                  <GitHubMark />
                </a>
              </Button>
            </div>
          </header>

          <main className={cn("min-h-0 flex-1 overflow-y-auto", isFixedWorkspace && "overflow-hidden")}>
            <div className={cn("mx-auto w-full max-w-[1360px] px-4 py-5 sm:px-6 lg:px-8", isFixedWorkspace ? "h-full py-4 lg:py-5" : "lg:py-7")}>
              <Outlet />
            </div>
            {!isMediaWorkspace ? <SiteFooter /> : null}
          </main>
        </div>
      </div>

      <Dialog open={passwordOpen} onOpenChange={setPasswordOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{t("auth.changePassword")}</DialogTitle><DialogDescription>{admin?.username}</DialogDescription></DialogHeader>
          <form className="space-y-4" onSubmit={passwordForm.handleSubmit(submitPassword)}>
            <div className="space-y-2"><Label htmlFor="current-password">{t("auth.currentPassword")}</Label><Input id="current-password" type="password" autoComplete="current-password" {...passwordForm.register("currentPassword")} />{passwordForm.formState.errors.currentPassword ? <p className="text-xs text-destructive">{passwordForm.formState.errors.currentPassword.message}</p> : null}</div>
            <div className="space-y-2"><Label htmlFor="new-password">{t("auth.newPassword")}</Label><Input id="new-password" type="password" autoComplete="new-password" {...passwordForm.register("newPassword")} />{passwordForm.formState.errors.newPassword ? <p className="text-xs text-destructive">{passwordForm.formState.errors.newPassword.message}</p> : null}</div>
            <DialogFooter><Button type="button" variant="secondary" size="sm" onClick={() => setPasswordOpen(false)}>{t("common.cancel")}</Button><Button type="submit" size="sm" disabled={passwordForm.formState.isSubmitting}>{t("common.save")}</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={attributionOpen} onOpenChange={(open) => (open ? setAttributionOpen(true) : closeAttribution())}>
        <DialogContent className="max-w-[560px]">
          <DialogHeader>
            <div className="mb-2 flex size-10 items-center justify-center rounded-xl bg-secondary text-foreground">
              <HeartHandshake className="size-5" strokeWidth={1.8} />
            </div>
            <DialogTitle>{t("community.noticeTitle")}</DialogTitle>
            <DialogDescription>{t("community.noticeDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 rounded-xl border border-border/70 bg-secondary/35 p-4 text-sm">
            <div className="flex items-center justify-between gap-4">
              <span className="text-muted-foreground">{t("community.noticeUpstream")}</span>
              <a className="inline-flex items-center gap-1.5 font-medium hover:underline" href="https://github.com/chenyme/grok2api" target="_blank" rel="noreferrer">
                chenyme/grok2api <ExternalLink className="size-3.5" />
              </a>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span className="text-muted-foreground">{t("community.noticeCustom")}</span>
              <a className="inline-flex items-center gap-1.5 font-medium hover:underline" href="https://github.com/LXXYSLF/grok-account-manager-main" target="_blank" rel="noreferrer">
                Grok 2api <ExternalLink className="size-3.5" />
              </a>
            </div>
          </div>
          <DialogFooter>
            <Button size="sm" onClick={closeAttribution}>{t("community.noticeAcknowledge")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
