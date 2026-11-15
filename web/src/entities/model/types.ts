export type ModelCapability =
  | "chat"
  | "responses"
  | "image"
  | "image_edit"
  | "video"
  | "stt"
  | "tts"
  | "realtime";

export type ModelRouteDTO = {
  id: string;
  publicId: string;
  provider: string;
  upstreamModel: string;
  capability: ModelCapability;
  origin: "catalog";
  enabled: boolean;
  accountIds: string[];
  bindingMode: boolean;
  supportedAccounts: number;
  syncedAccounts: number;
  totalAccounts: number;
  capabilityKnown: boolean;
  available: boolean;
  lastSyncedAt?: string;
  weight: number;
  config: Record<string, unknown>;
};
