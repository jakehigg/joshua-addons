import { Fragment, useEffect, useState } from "react";
import { api, type PurchaseRecord } from "../api";

const PAGE_SIZE = 100;

const currencyFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function fmtMoney(value: number | null): string {
  if (value === null) return "—";
  return currencyFmt.format(value);
}

function fmtDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// The poller writes source "automatic"; manual/receipt paths write "manual".
function sourceLabel(source: string): string {
  return source === "automatic" ? "Poller" : "Manual";
}

interface Props {
  // Called after a delete/edit so the inventory-derived tabs (Pantry,
  // Analytics) pick up the recalculated stats.
  onRefresh: () => void;
}

export function Admin({ onRefresh }: Props) {
  const [rows, setRows] = useState<PurchaseRecord[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  // Inline edit state — one row at a time.
  const [editId, setEditId] = useState<number | null>(null);
  const [editCost, setEditCost] = useState("");
  const [editStore, setEditStore] = useState("");
  const [saving, setSaving] = useState(false);

  // Two-step delete: first click arms, second click confirms.
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  useEffect(() => {
    api.listPurchases(PAGE_SIZE, 0)
      .then((res) => { setRows(res.purchases); setTotal(res.total); setError(null); })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const hasMore = rows !== null && rows.length < total;

  function loadMore() {
    if (!rows) return;
    setLoadingMore(true);
    api.listPurchases(PAGE_SIZE, rows.length)
      .then((res) => {
        setRows((prev) => [...(prev ?? []), ...res.purchases]);
        setTotal(res.total);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingMore(false));
  }

  function startEdit(p: PurchaseRecord) {
    setConfirmDeleteId(null);
    setEditId(p.id);
    setEditCost(p.unit_cost === null ? "" : String(p.unit_cost));
    setEditStore(p.store ?? "");
  }

  function cancelEdit() {
    setEditId(null);
    setEditCost("");
    setEditStore("");
  }

  async function saveEdit(p: PurchaseRecord) {
    const trimmedCost = editCost.trim();
    let unit_cost: number | null = null;
    if (trimmedCost !== "") {
      const parsed = Number(trimmedCost);
      if (Number.isNaN(parsed) || parsed < 0) {
        setError("Cost must be a non-negative number.");
        return;
      }
      unit_cost = parsed;
    }
    const store = editStore.trim() === "" ? null : editStore.trim();

    setSaving(true);
    try {
      const updated = await api.editPurchase(p.id, { unit_cost, store });
      setRows((prev) => (prev ?? []).map((r) => (r.id === p.id ? updated : r)));
      setError(null);
      cancelEdit();
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(p: PurchaseRecord) {
    if (confirmDeleteId !== p.id) {
      setEditId(null);
      setConfirmDeleteId(p.id);
      return;
    }
    setBusyId(p.id);
    try {
      await api.deletePurchase(p.id);
      setRows((prev) => (prev ?? []).filter((r) => r.id !== p.id));
      setTotal((t) => Math.max(0, t - 1));
      setConfirmDeleteId(null);
      setError(null);
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  if (error && rows === null) return <div className="error">{error}</div>;
  if (rows === null) return <div className="j-empty loading"><span className="j-spinner" />Loading…</div>;
  if (rows.length === 0) return <div className="j-empty">No purchases recorded yet.</div>;

  return (
    <section className="stock-section">
      <h2 className="j-label">
        All purchases <span className="j-count">{total}</span>
      </h2>

      {error && <div className="error">{error}</div>}

      <div className="j-table-wrap">
        <table className="j-table data-table">
          <thead>
            <tr>
              <th className="ta-left">Item</th>
              <th className="ta-left">Purchased</th>
              <th className="num">Cost</th>
              <th className="ta-left">Store</th>
              <th className="ta-left">Source</th>
              <th className="num">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const editing = editId === p.id;
              const confirming = confirmDeleteId === p.id;
              const busy = busyId === p.id;
              return (
                <Fragment key={p.id}>
                  <tr>
                    <td className="ta-left">{p.item_name}</td>
                    <td className="ta-left nowrap">{fmtDateTime(p.purchased_at)}</td>
                    <td className="num">{fmtMoney(p.unit_cost)}</td>
                    <td className="ta-left">{p.store ?? "—"}</td>
                    <td className="ta-left">
                      <span className={`j-badge plain${p.source === "automatic" ? " accent" : ""}`}>{sourceLabel(p.source)}</span>
                    </td>
                    <td className="num">
                      <div className="item-actions">
                        <button
                          className="j-btn sm"
                          onClick={() => (editing ? cancelEdit() : startEdit(p))}
                          disabled={busy}
                        >
                          {editing ? "Close" : "Edit"}
                        </button>
                        <button
                          className={`j-btn sm danger${confirming ? " armed" : ""}`}
                          onClick={() => handleDelete(p)}
                          disabled={busy}
                        >
                          {busy ? "…" : confirming ? "Confirm?" : "Delete"}
                        </button>
                      </div>
                    </td>
                  </tr>
                  {editing && (
                    <tr className="analytics-subrow">
                      <td className="ta-left" colSpan={6}>
                        <div className="inline-form admin-edit">
                          <label className="j-field-label">
                            <span>Cost</span>
                            <span className="j-field sm">
                            <input
                              type="number"
                              inputMode="decimal"
                              step="0.01"
                              min="0"
                              placeholder="unset"
                              value={editCost}
                              onChange={(e) => setEditCost(e.target.value)}
                            />
                            </span>
                          </label>
                          <label className="j-field-label">
                            <span>Store</span>
                            <span className="j-field sm">
                            <input
                              type="text"
                              placeholder="unset"
                              value={editStore}
                              onChange={(e) => setEditStore(e.target.value)}
                            />
                            </span>
                          </label>
                          <button className="j-btn primary sm" onClick={() => saveEdit(p)} disabled={saving}>
                            {saving ? "Saving…" : "Save"}
                          </button>
                          <button className="j-btn ghost sm" onClick={cancelEdit} disabled={saving}>
                            Cancel
                          </button>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {hasMore && (
        <div className="admin-load-more">
          <button className="j-btn" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : `Load more (${rows.length} of ${total})`}
          </button>
        </div>
      )}
    </section>
  );
}
