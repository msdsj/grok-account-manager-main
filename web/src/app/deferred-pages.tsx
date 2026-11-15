import { lazy, Suspense, type ComponentType, type LazyExoticComponent } from "react";

import { Spinner } from "@/components/ui/spinner";

const AppShell = lazyNamed(() => import("@/app/app-shell"), "AppShell");
const RequestAuditsPage = lazyNamed(() => import("@/features/audits/request-audits-page"), "RequestAuditsPage");
const ClientKeysPage = lazyNamed(() => import("@/features/client-keys/client-keys-page"), "ClientKeysPage");
const CreativeConsolePage = lazyNamed(() => import("@/features/creative-console/creative-console-page"), "CreativeConsolePage");
const DashboardPage = lazyNamed(() => import("@/features/dashboard/dashboard-page"), "DashboardPage");
const ApiDocsPage = lazyNamed(() => import("@/features/docs/api-docs-page"), "ApiDocsPage");
const ModelsPage = lazyNamed(() => import("@/features/models/models-page"), "ModelsPage");
const ManagedAccountsPage = lazyNamed(() => import("@/features/managed-accounts/managed-accounts-page"), "ManagedAccountsPage");
const GrokAccountPoolPage = lazyNamed(() => import("@/features/managed-accounts/account-pool-page"), "GrokAccountPoolPage");
const KiroAccountPoolPage = lazyNamed(() => import("@/features/managed-accounts/account-pool-page"), "KiroAccountPoolPage");
const GptAccountPoolPage = lazyNamed(() => import("@/features/managed-accounts/account-pool-page"), "GptAccountPoolPage");
const AccountTestBoardPage = lazyNamed(() => import("@/features/account-tests/account-test-board-page"), "AccountTestBoardPage");
const OperationLogsPage = lazyNamed(() => import("@/features/logs/operation-logs-page"), "OperationLogsPage");
const RegisterTaskPage = lazyNamed(() => import("@/features/register-task/register-task-page"), "RegisterTaskPage");
const SettingsPage = lazyNamed(() => import("@/features/settings/settings-page"), "SettingsPage");

function lazyNamed<T extends Record<K, ComponentType>, K extends keyof T>(loader: () => Promise<T>, exportName: K): LazyExoticComponent<T[K]> {
  return lazy(async () => ({ default: (await loader())[exportName] }));
}

function DeferredPage({ page: Page }: { page: ComponentType }) {
  return <Suspense fallback={<PageLoadingFallback />}><Page /></Suspense>;
}

export function DeferredAppShell() {
  return <Suspense fallback={<PageLoadingFallback fullScreen />}><AppShell /></Suspense>;
}

export function DeferredDashboardPage() {
  return <DeferredPage page={DashboardPage} />;
}

export function DeferredModelsPage() {
  return <DeferredPage page={ModelsPage} />;
}

export function DeferredManagedAccountsPage() {
  return <DeferredPage page={ManagedAccountsPage} />;
}

export function DeferredGrokAccountPoolPage() {
  return <DeferredPage page={GrokAccountPoolPage} />;
}

export function DeferredKiroAccountPoolPage() {
  return <DeferredPage page={KiroAccountPoolPage} />;
}

export function DeferredGptAccountPoolPage() {
  return <DeferredPage page={GptAccountPoolPage} />;
}

export function DeferredAccountTestBoardPage() {
  return <DeferredPage page={AccountTestBoardPage} />;
}

export function DeferredOperationLogsPage() {
  return <DeferredPage page={OperationLogsPage} />;
}

export function DeferredRegisterTaskPage() {
  return <DeferredPage page={RegisterTaskPage} />;
}

export function DeferredClientKeysPage() {
  return <DeferredPage page={ClientKeysPage} />;
}

export function DeferredCreativeConsolePage() {
  return <DeferredPage page={CreativeConsolePage} />;
}

export function DeferredRequestAuditsPage() {
  return <DeferredPage page={RequestAuditsPage} />;
}

export function DeferredApiDocsPage() {
  return <DeferredPage page={ApiDocsPage} />;
}

export function DeferredSettingsPage() {
  return <DeferredPage page={SettingsPage} />;
}

function PageLoadingFallback({ fullScreen = false }: { fullScreen?: boolean }) {
  return (
    <div className={fullScreen ? "flex min-h-screen items-center justify-center bg-background" : "flex min-h-[calc(100vh-7rem)] items-center justify-center lg:min-h-[calc(100vh-10rem)]"}>
      <Spinner className="size-5" />
    </div>
  );
}
