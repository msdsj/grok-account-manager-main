import { ManagedAccountsPage } from "@/features/managed-accounts/managed-accounts-page";

export function GrokAccountPoolPage() {
  return <ManagedAccountsPage fixedTab="grok" />;
}

export function KiroAccountPoolPage() {
  return <ManagedAccountsPage fixedTab="kiro" />;
}

export function GptAccountPoolPage() {
  return <ManagedAccountsPage fixedTab="gpt" />;
}
