import { Fragment, useEffect, useMemo, useState } from "react";
import { api, type InventoryItem, type PurchaseAnalytics } from "../api";
import { AliasEditor, ItemActions, RenameForm, type ItemControls } from "./itemControls";

function fmtDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function fmtDays(days: number | null): string {
  if (days === null) return "—";
  return `${days}d`;
}

const currencyFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function fmtMoney(value: number | null): string {
  if (value === null) return "—";
  return currencyFmt.format(value);
}

type SortKey =
  | "name"
  | "purchase_count"
  | "shortest_interval_days"
  | "median_interval_days"
  | "longest_interval_days"
  | "cycle_days"
  | "avg_cost"
  | "last_purchased_at";

type SortDir = "asc" | "desc";

interface Column {
  key: SortKey;
  label: string;
  title?: string;
  align: "left" | "right";
}

const COLUMNS: Column[] = [
  { key: "name", label: "Item", align: "left" },
  { key: "purchase_count", label: "# bought", title: "Number of recorded purchases", align: "right" },
  { key: "shortest_interval_days", label: "Shortest", title: "Shortest gap between consecutive purchases", align: "right" },
  { key: "median_interval_days", label: "Median", title: "Median gap — drives the depletion estimate", align: "right" },
  { key: "longest_interval_days", label: "Longest", title: "Longest gap between consecutive purchases", align: "right" },
  { key: "cycle_days", label: "Cycle (used)", title: "Cycle length used by the live depletion algorithm", align: "right" },
  { key: "avg_cost", label: "Avg cost", title: "Average recorded price paid per purchase", align: "right" },
  { key: "last_purchased_at", label: "Last bought", align: "left" },
];

// Total table width including the trailing (non-sortable) Actions column.
const COL_SPAN = COLUMNS.length + 1;

function sortValue(r: PurchaseAnalytics, key: SortKey): string | number | null {
  switch (key) {
    case "name":
      return r.name.toLowerCase();
    case "last_purchased_at":
      return r.last_purchased_at ? new Date(r.last_purchased_at).getTime() : null;
    default:
      return r[key];
  }
}

interface Props {
  controls: ItemControls;
  // Bumped by the parent after any item action so we re-fetch our own data.
  refreshKey: number;
}

export function Analytics({ controls, refreshKey }: Props) {
  const [rows, setRows] = useState<PurchaseAnalytics[] | null>(null);
  // Live stock status per item — analytics rows don't carry it, but the
  // out-of-stock control needs it. Keyed by item_id.
  const [statusById, setStatusById] = useState<Map<number, InventoryItem["status"]>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const { mergeSource, expandedId, renameId, refresh, onNameTap, onMergeStart, onRenameStart, onRenameEnd } = controls;
  const mergeMode = mergeSource !== null;

  useEffect(() => {
    Promise.all([api.getAnalytics(), api.getInventory()])
      .then(([analytics, inventory]) => {
        setRows(analytics);
        setStatusById(new Map(inventory.map((i) => [i.item_id, i.status])));
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [refreshKey]);

  // Preserve the API's default order until a column is explicitly chosen.
  const sortedRows = useMemo(() => {
    if (!rows || sortKey === null) return rows;
    return [...rows].sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      // Missing values always sort to the bottom, regardless of direction.
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
  }, [rows, sortKey, sortDir]);

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  if (error) return <div className="error">{error}</div>;
  if (rows === null || sortedRows === null) return <div className="empty"><span className="spinner" />Loading…</div>;
  if (sortedRows.length === 0) return <div className="empty">No tracked items yet.</div>;

  return (
    <div className="panel">
      <div className="panel-header">
        <h2>Purchase Analytics</h2>
        <span className="badge">{sortedRows.length}</span>
      </div>
      <div className="analytics-scroll">
        <table className="analytics-table">
          <thead>
            <tr>
              {COLUMNS.map((col) => {
                const active = sortKey === col.key;
                return (
                  <th
                    key={col.key}
                    className={`sortable${col.align === "left" ? " ta-left" : ""}${active ? " sorted" : ""}`}
                    title={col.title}
                    onClick={() => handleSort(col.key)}
                    aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
                  >
                    {col.label}
                    <span className="sort-arrow">{active ? (sortDir === "asc" ? "▲" : "▼") : ""}</span>
                  </th>
                );
              })}
              <th className="ta-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((r) => {
              const status = statusById.get(r.item_id) ?? "unknown";
              const isMergeSource = mergeSource?.item_id === r.item_id;
              const expanded = expandedId === r.item_id;
              const renaming = renameId === r.item_id;
              return (
                <Fragment key={r.item_id}>
                  <tr
                    className={
                      (isMergeSource ? "analytics-row--merge-source" : "") +
                      (mergeMode && !isMergeSource ? " analytics-row--merge-target" : "")
                    }
                  >
                    <td className="ta-left">
                      <span className="analytics-name item-name--tappable" onClick={() => onNameTap(r)}>
                        {r.name}
                      </span>
                      {r.aliases.length > 0 && (
                        <span className="analytics-aka" title={r.aliases.join(", ")}>
                          aka {r.aliases.join(", ")}
                        </span>
                      )}
                    </td>
                    <td>{r.purchase_count}</td>
                    <td>{fmtDays(r.shortest_interval_days)}</td>
                    <td className="ta-strong">{fmtDays(r.median_interval_days)}</td>
                    <td>{fmtDays(r.longest_interval_days)}</td>
                    <td>{fmtDays(r.cycle_days)}</td>
                    <td>{fmtMoney(r.avg_cost)}</td>
                    <td className="ta-left">{fmtDate(r.last_purchased_at)}</td>
                    <td className="ta-right">
                      {!mergeMode && (
                        <div className="analytics-actions">
                          <ItemActions
                            item={{ item_id: r.item_id, name: r.name, status }}
                            onRefresh={refresh}
                            onMergeStart={() => onMergeStart(r)}
                            onRenameStart={() => onRenameStart(r)}
                            renaming={renaming}
                          />
                        </div>
                      )}
                    </td>
                  </tr>
                  {expanded && !mergeMode && (
                    <tr className="analytics-subrow">
                      <td className="ta-left" colSpan={COL_SPAN}>
                        <AliasEditor itemId={r.item_id} onChanged={refresh} />
                      </td>
                    </tr>
                  )}
                  {renaming && !mergeMode && (
                    <tr className="analytics-subrow">
                      <td className="ta-left" colSpan={COL_SPAN}>
                        <RenameForm
                          item={r}
                          onSuccess={() => { onRenameEnd(); refresh(); }}
                          onCancel={onRenameEnd}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
