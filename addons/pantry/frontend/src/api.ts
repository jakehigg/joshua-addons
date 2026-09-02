export interface InventoryItem {
  item_id: number;
  name: string;
  aliases: string[];
  category: string | null;
  status: "in_stock" | "likely_depleted" | "unknown" | "out_of_stock";
  last_purchased_at: string | null;
  avg_cycle_days: number | null;
  estimated_depletion: string | null;
  updated_at: string | null;
}

export interface PurchaseAnalytics {
  item_id: number;
  name: string;
  aliases: string[];
  category: string | null;
  purchase_count: number;
  first_purchased_at: string | null;
  last_purchased_at: string | null;
  shortest_interval_days: number | null;
  median_interval_days: number | null;
  longest_interval_days: number | null;
  cycle_days: number | null;
  avg_cost: number | null;
  last_cost: number | null;
}

export interface PurchaseRecord {
  id: number;
  item_id: number;
  item_name: string;
  purchased_at: string;
  purchase_date: string;
  source: string;
  unit_cost: number | null;
  store: string | null;
}

export interface PurchaseListResponse {
  purchases: PurchaseRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface AliasEntry {
  id: number;
  alias: string;
}

export interface MergeResult {
  target_id: number;
  target_name: string;
  alias_added: string | null;
  moved_purchases: number;
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch { /* non-JSON error body */ }
    throw new Error(`API error ${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getInventory: () => apiFetch<InventoryItem[]>("/api/inventory"),
  getAnalytics: () => apiFetch<PurchaseAnalytics[]>("/api/analytics"),
  markOutOfStock: (itemId: number) =>
    apiFetch<{ item_name: string }>(`/api/inventory/${itemId}/mark-out-of-stock`, { method: "POST" }),
  deactivateItem: (itemId: number) =>
    apiFetch<{ ok: boolean }>(`/api/items/${itemId}`, { method: "DELETE" }),
  recordPurchase: (name: string) =>
    apiFetch<{ item_name: string; created: boolean }>("/api/inventory/record-purchase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  getAliases: (itemId: number) => apiFetch<AliasEntry[]>(`/api/items/${itemId}/aliases`),
  addAlias: (itemId: number, alias: string) =>
    apiFetch<AliasEntry>(`/api/items/${itemId}/aliases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias }),
    }),
  removeAlias: (itemId: number, aliasId: number) =>
    apiFetch<{ ok: boolean }>(`/api/items/${itemId}/aliases/${aliasId}`, { method: "DELETE" }),
  mergeItems: (sourceId: number, targetId: number) =>
    apiFetch<MergeResult>(`/api/items/${sourceId}/merge-into/${targetId}`, { method: "POST" }),
  renameItem: (itemId: number, newName: string) =>
    apiFetch<{ id: number; name: string }>(`/api/items/${itemId}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: newName }),
    }),
  listPurchases: (limit: number, offset: number) =>
    apiFetch<PurchaseListResponse>(`/api/purchases?limit=${limit}&offset=${offset}`),
  editPurchase: (purchaseId: number, changes: { unit_cost?: number | null; store?: string | null }) =>
    apiFetch<PurchaseRecord>(`/api/purchases/${purchaseId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),
  deletePurchase: (purchaseId: number) =>
    apiFetch<{ ok: boolean }>(`/api/purchases/${purchaseId}`, { method: "DELETE" }),
};
