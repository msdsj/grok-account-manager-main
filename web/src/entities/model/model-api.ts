import type { ModelRouteDTO } from "@/entities/model/types";
import { listGatewayModels, type GatewayModelDTO } from "@/shared/api/gateway";
import type { PaginatedDTO } from "@/shared/api/client";
import type { SortOrder } from "@/shared/lib/table-sort";

type ListModelsInput = {
  page: number;
  pageSize: number;
  search?: string;
  status?: string;
  provider?: string;
  providerScope?: string[];
  tierScope?: string[];
  activeScope?: boolean;
  sortBy?: string;
  sortOrder?: SortOrder;
};

const providerAliases: Record<string, string> = {
  grok_build: "grok",
  grok_web: "grok",
  grok_console: "grok",
};

function toModelRoute(model: GatewayModelDTO, index: number): ModelRouteDTO {
  const enabled = model.enabled !== false;
  return {
    id: `${model.provider}:${model.publicModel}:${index}`,
    publicId: model.publicModel,
    provider: model.provider,
    upstreamModel: model.upstreamModel,
    capability: model.capability as ModelRouteDTO["capability"],
    origin: "catalog",
    enabled,
    accountIds: [],
    bindingMode: false,
    supportedAccounts: enabled ? 1 : 0,
    syncedAccounts: enabled ? 1 : 0,
    totalAccounts: 1,
    capabilityKnown: true,
    available: enabled,
    weight: typeof model.weight === "number" ? model.weight : 1,
    config: typeof model.config === "object" && model.config !== null ? model.config : {},
  };
}

export async function listModels(input: ListModelsInput): Promise<PaginatedDTO<ModelRouteDTO>> {
  const models = await listGatewayModels();
  let items = models.map(toModelRoute);
  if (input.status === "enabled") items = items.filter((model) => model.enabled);
  if (input.status === "disabled") items = items.filter((model) => !model.enabled);
  if (input.provider) {
    const provider = providerAliases[input.provider] ?? input.provider;
    items = items.filter((model) => model.provider === provider);
  }
  const search = input.search?.trim().toLocaleLowerCase();
  if (search) {
    items = items.filter((model) => (
      model.publicId.toLocaleLowerCase().includes(search)
      || model.upstreamModel.toLocaleLowerCase().includes(search)
    ));
  }
  const page = Math.max(1, input.page);
  const pageSize = Math.max(1, input.pageSize);
  const total = items.length;
  return {
    items: items.slice((page - 1) * pageSize, page * pageSize),
    page,
    pageSize,
    total,
  };
}
