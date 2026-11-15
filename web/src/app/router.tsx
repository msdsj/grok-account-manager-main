import { Navigate, createBrowserRouter } from "react-router-dom";

import { AnonymousBoundary, AuthBoundary } from "@/app/auth-boundary";
import { DeferredApiDocsPage, DeferredAppShell, DeferredClientKeysPage, DeferredCreativeConsolePage, DeferredDashboardPage, DeferredManagedAccountsPage, DeferredModelsPage, DeferredOperationLogsPage, DeferredRegisterTaskPage, DeferredRequestAuditsPage, DeferredSettingsPage } from "@/app/deferred-pages";
import { LoginPage } from "@/features/auth/login-page";

export const router = createBrowserRouter([
  {
    element: <AnonymousBoundary />,
    children: [{ path: "/login", element: <LoginPage /> }],
  },
  {
    element: <AuthBoundary />,
    children: [
      {
        element: <DeferredAppShell />,
        children: [
          { index: true, element: <Navigate to="/dashboard" replace /> },
          { path: "/dashboard", element: <DeferredDashboardPage /> },
          { path: "/register", element: <DeferredRegisterTaskPage /> },
          { path: "/accounts", element: <Navigate to="/managed-accounts" replace /> },
          { path: "/managed-accounts", element: <DeferredManagedAccountsPage /> },
          { path: "/operation-logs", element: <DeferredOperationLogsPage /> },
          { path: "/models", element: <DeferredModelsPage /> },
          { path: "/creative-console", element: <DeferredCreativeConsolePage /> },
          { path: "/client-keys", element: <DeferredClientKeysPage /> },
          { path: "/request-audits", element: <DeferredRequestAuditsPage /> },
          { path: "/docs", element: <Navigate to="/docs/chat/completions" replace /> },
          { path: "/docs/:category/:endpoint", element: <DeferredApiDocsPage /> },
          { path: "/settings", element: <DeferredSettingsPage /> },
        ],
      },
    ],
  },
  { path: "*", element: <Navigate to="/dashboard" replace /> },
]);
