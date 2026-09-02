import { useEffect, useState } from "react";
import { api, type AliasEntry, type InventoryItem } from "../api";

// Minimal shape needed to start a merge/rename or be a merge target. Both
// InventoryItem (Pantry tab) and PurchaseAnalytics (Analytics tab) satisfy it.
export type ItemRef = { item_id: number; name: string };

// Per-item action buttons need the live stock status to decide whether the
// "mark out of stock" control applies.
export type ActionableItem = ItemRef & { status: InventoryItem["status"] };

/**
 * Shared per-item control state + handlers used by both the Pantry tab and the
 * Analytics tab. Merge/rename/expand state lives here so a merge started on one
 * tab can be completed on the other, and so both tabs render identical controls.
 *
 * `refresh` should re-fetch whatever data the consuming tabs display.
 */
export function useItemControls(refresh: () => void) {
  const [mergeSource, setMergeSource] = useState<ItemRef | null>(null);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [renameId, setRenameId] = useState<number | null>(null);

  const onRenameStart = (item: ItemRef) => {
    setRenameId(item.item_id);
    setExpandedId(null);
  };
  const onRenameEnd = () => setRenameId(null);

  const onMergeStart = (item: ItemRef) => {
    setMergeSource(item);
    setExpandedId(null);
    setRenameId(null);
    setMergeError(null);
  };

  const cancelMerge = () => setMergeSource(null);
  const clearMergeError = () => setMergeError(null);

  const onNameTap = async (item: ItemRef) => {
    setRenameId(null);
    if (mergeSource) {
      if (item.item_id === mergeSource.item_id) return;
      const ok = window.confirm(
        `Merge "${mergeSource.name}" into "${item.name}"?\n\nPurchase history will be combined and "${mergeSource.name}" becomes an alias of "${item.name}".`
      );
      if (ok) {
        try {
          await api.mergeItems(mergeSource.item_id, item.item_id);
          setMergeError(null);
        } catch (err) {
          setMergeError(err instanceof Error ? err.message : String(err));
        }
        setMergeSource(null);
        refresh();
      }
      return;
    }
    setExpandedId((cur) => (cur === item.item_id ? null : item.item_id));
  };

  // Clear transient per-row state (used when switching tabs). Merge state is
  // intentionally preserved so a merge can span tabs.
  const resetTransient = () => {
    setExpandedId(null);
    setRenameId(null);
  };

  return {
    mergeSource,
    mergeError,
    expandedId,
    renameId,
    refresh,
    onNameTap,
    onMergeStart,
    onRenameStart,
    onRenameEnd,
    cancelMerge,
    clearMergeError,
    resetTransient,
  };
}

export type ItemControls = ReturnType<typeof useItemControls>;

export function AliasEditor({ itemId, onChanged }: { itemId: number; onChanged: () => void }) {
  const [aliases, setAliases] = useState<AliasEntry[] | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    api.getAliases(itemId).then(setAliases).catch(() => setAliases([]));
  };
  useEffect(load, [itemId]);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.addAlias(itemId, name.trim());
      setName("");
      load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (aliasId: number) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.removeAlias(itemId, aliasId);
      load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="alias-editor">
      {aliases === null ? (
        <span className="alias-loading">Loading aliases…</span>
      ) : (
        <div className="alias-chips">
          {aliases.length === 0 && <span className="alias-empty">No aliases yet</span>}
          {aliases.map((a) => (
            <span key={a.id} className="alias-chip">
              {a.alias}
              <button
                className="alias-chip-remove"
                onClick={() => handleRemove(a.id)}
                disabled={busy}
                title="Remove alias"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <form className="alias-form" onSubmit={handleAdd}>
        <input
          className="alias-input"
          type="text"
          placeholder="Add alias (aka name)…"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={busy}
        />
        <button type="submit" className="alias-add-btn" disabled={!name.trim() || busy}>
          +
        </button>
      </form>
      {error && <div className="alias-error">{error}</div>}
    </div>
  );
}

interface RenameFormProps {
  item: ItemRef;
  onSuccess: () => void;
  onCancel: () => void;
}

export function RenameForm({ item, onSuccess, onCancel }: RenameFormProps) {
  const [value, setValue] = useState(item.name);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || state === "loading") return;
    setState("loading");
    setError(null);
    try {
      await api.renameItem(item.item_id, trimmed);
      onSuccess();
    } catch (err) {
      setState("error");
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="rename-form">
      <form className="rename-form-inner" onSubmit={handleSubmit}>
        <input
          className="rename-input"
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={state === "loading"}
          autoFocus
        />
        <button type="submit" className="rename-confirm-btn" disabled={!value.trim() || state === "loading"}>
          {state === "loading" ? "…" : "✓"}
        </button>
        <button type="button" className="rename-cancel-btn" onClick={onCancel} disabled={state === "loading"}>
          ✕
        </button>
      </form>
      {error && <div className="rename-error">{error}</div>}
    </div>
  );
}

interface ItemActionsProps {
  item: ActionableItem;
  onRefresh: () => void;
  onMergeStart: () => void;
  onRenameStart: () => void;
  renaming: boolean;
}

/**
 * The per-item action buttons (out-of-stock, merge, rename, hide). Shared
 * between the Pantry tab rows and the Analytics table rows so the two stay
 * in sync. Button-press feedback state is local to each instance.
 */
export function ItemActions({ item, onRefresh, onMergeStart, onRenameStart, renaming }: ItemActionsProps) {
  const [ooState, setOoState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [hideState, setHideState] = useState<"idle" | "loading" | "error">("idle");

  const handleOutOfStock = async () => {
    if (ooState !== "idle") return;
    setOoState("loading");
    try {
      await api.markOutOfStock(item.item_id);
      setOoState("done");
      setTimeout(() => onRefresh(), 300);
    } catch {
      setOoState("error");
      setTimeout(() => setOoState("idle"), 2000);
    }
  };

  const handleHide = async () => {
    if (hideState !== "idle") return;
    setHideState("loading");
    try {
      await api.deactivateItem(item.item_id);
      onRefresh();
    } catch {
      setHideState("error");
      setTimeout(() => setHideState("idle"), 2000);
    }
  };

  return (
    <>
      {(item.status === "in_stock" || item.status === "likely_depleted") && (
        <button
          className={`oos-btn oos-btn--${ooState}`}
          onClick={handleOutOfStock}
          disabled={ooState !== "idle"}
          title="Mark as out of stock"
        >
          {ooState === "loading" ? "…" : ooState === "error" ? "!" : "−"}
        </button>
      )}
      <button
        className="merge-btn"
        onClick={onMergeStart}
        title="Merge this item into another"
      >
        ⇆
      </button>
      <button
        className={`rename-btn${renaming ? " rename-btn--active" : ""}`}
        onClick={onRenameStart}
        title="Rename this item"
      >
        ✎
      </button>
      <button
        className={`hide-btn hide-btn--${hideState}`}
        onClick={handleHide}
        disabled={hideState !== "idle"}
        title="Hide this item"
      >
        {hideState === "loading" ? "…" : hideState === "error" ? "!" : "🗑"}
      </button>
    </>
  );
}
