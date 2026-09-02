import { Fragment, useRef, useState } from "react";
import { api, type InventoryItem } from "../api";
import { AliasEditor, ItemActions, RenameForm, type ItemControls } from "./itemControls";

function fmtDate(dateStr: string | null): string {
  if (!dateStr) return "";
  return new Date(dateStr).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function daysUntil(dateStr: string | null): string {
  if (!dateStr) return "";
  const diff = (new Date(dateStr).getTime() - Date.now()) / 86400000;
  if (diff <= 0) return "depleted";
  if (diff < 1) return "today";
  return `~${Math.round(diff)}d left`;
}

interface ItemRowProps {
  item: InventoryItem;
  controls: ItemControls;
}

function ItemRow({ item, controls }: ItemRowProps) {
  const { mergeSource, expandedId, renameId, refresh, onNameTap, onMergeStart, onRenameStart, onRenameEnd } = controls;

  const isMergeSource = mergeSource?.item_id === item.item_id;
  const mergeMode = mergeSource !== null;
  const expanded = expandedId === item.item_id;
  const renaming = renameId === item.item_id;

  const dotClass =
    item.status === "in_stock" ? "dot-green" :
    item.status === "out_of_stock" ? "dot-red" :
    item.status === "likely_depleted" ? "dot-amber" :
    "dot-gray";

  const statusMeta =
    item.status === "in_stock" && item.estimated_depletion
      ? daysUntil(item.estimated_depletion)
      : null;

  const purchasedMeta = item.last_purchased_at
    ? `bought ${fmtDate(item.last_purchased_at)}`
    : "never purchased";

  return (
    <li className={`item-card${isMergeSource ? " item-card--merge-source" : ""}${mergeMode && !isMergeSource ? " item-card--merge-target" : ""}`}>
      <div className="item-row">
        <div className={`dot ${dotClass}`} />
        <span className="item-name item-name--tappable" onClick={() => onNameTap(item)}>
          {item.name}
          {item.aliases.length > 0 && <span className="aka-tag">aka {item.aliases.length}</span>}
        </span>
        {statusMeta && <span className="item-meta">{statusMeta}</span>}
        <span className="item-meta item-purchased">{purchasedMeta}</span>
        {!mergeMode && (
          <ItemActions
            item={item}
            onRefresh={refresh}
            onMergeStart={() => onMergeStart(item)}
            onRenameStart={() => onRenameStart(item)}
            renaming={renaming}
          />
        )}
      </div>
      {expanded && !mergeMode && (
        <AliasEditor itemId={item.item_id} onChanged={refresh} />
      )}
      {renaming && !mergeMode && (
        <RenameForm
          item={item}
          onSuccess={() => { onRenameEnd(); refresh(); }}
          onCancel={onRenameEnd}
        />
      )}
    </li>
  );
}

export function RecordPurchaseForm({ onRefresh }: { onRefresh: () => void }) {
  const [name, setName] = useState("");
  const [state, setState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || state !== "idle") return;
    setState("loading");
    try {
      await api.recordPurchase(name.trim());
      setName("");
      setState("done");
      onRefresh();
      setTimeout(() => {
        setState("idle");
        inputRef.current?.focus();
      }, 800);
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 2000);
    }
  };

  return (
    <form className="record-purchase-form" onSubmit={handleSubmit}>
      <input
        ref={inputRef}
        className="record-purchase-input"
        type="text"
        placeholder="Record a purchase…"
        value={name}
        onChange={(e) => setName(e.target.value)}
        disabled={state === "loading"}
      />
      <button
        type="submit"
        className={`record-purchase-btn record-purchase-btn--${state}`}
        disabled={!name.trim() || state !== "idle"}
      >
        {state === "loading" ? "…" : state === "done" ? "✓" : state === "error" ? "!" : "+"}
      </button>
    </form>
  );
}

interface Props {
  items: InventoryItem[];
  loading: boolean;
  controls: ItemControls;
}

// Tiled in-stock items, grouped by category, plus Out of Stock and Likely
// Depleted/Unknown panels below — everything the ported family UI showed
// across its old "grocery" and "pantry" tabs, now on one Pantry tab (the
// grocery-list panel itself came from Apple Reminders and is not part of
// this addon).
export function PantryInventory({ items, loading, controls }: Props) {
  const inStock = items.filter((i) => i.status === "in_stock");
  const outOfStock = items.filter((i) => i.status === "out_of_stock");
  const depleted = items.filter((i) => i.status === "likely_depleted");
  const unknown = items.filter((i) => i.status === "unknown");

  const makeRow = (item: InventoryItem) => (
    <ItemRow key={item.item_id} item={item} controls={controls} />
  );

  // Flat list with inline category headers — used for the Out of Stock and
  // Likely Depleted/Unknown panels.
  const renderList = (list: InventoryItem[]) => {
    const groups = new Map<string, InventoryItem[]>();
    for (const item of list) {
      const key = item.category ?? "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(item);
    }
    const categories = [...groups.keys()].filter((c) => c !== "").sort();
    if (groups.has("")) categories.push("");
    const showHeaders = categories.length > 1 || categories[0] !== "";

    return (
      <ul className="item-list">
        {categories.map((cat) => (
          <Fragment key={cat || "__uncategorized"}>
            {showHeaders && <li className="category-header">{cat || "Other"}</li>}
            {groups.get(cat)!.map(makeRow)}
          </Fragment>
        ))}
      </ul>
    );
  };

  // Tiled cards by category — used for in-stock items.
  const renderTiles = (list: InventoryItem[]) => {
    const groups = new Map<string, InventoryItem[]>();
    for (const item of list) {
      const key = item.category ?? "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(item);
    }
    const categories = [...groups.keys()].filter((c) => c !== "").sort();
    if (groups.has("")) categories.push("");

    if (categories.length === 0) {
      return <div className="empty">No items in stock.</div>;
    }

    // Sort tallest first for better greedy packing
    const sorted = [...categories].sort(
      (a, b) => groups.get(b)!.length - groups.get(a)!.length
    );

    // Greedy 2-column assignment
    const cols: string[][] = [[], []];
    const heights = [0, 0];
    for (const cat of sorted) {
      const col = heights[0] <= heights[1] ? 0 : 1;
      cols[col].push(cat);
      heights[col] += groups.get(cat)!.length;
    }

    return (
      <div className="tile-cols">
        {cols.map((colCats, ci) => (
          <div key={ci} className="tile-col">
            {colCats.map((cat) => (
              <div key={cat || "__other"} className="tile-card panel">
                <div className="panel-header">
                  <h2>{cat || "Other"}</h2>
                  <span className="badge">{groups.get(cat)!.length}</span>
                </div>
                <ul className="item-list">
                  {groups.get(cat)!.map(makeRow)}
                </ul>
              </div>
            ))}
          </div>
        ))}
      </div>
    );
  };

  if (loading) {
    return <div className="empty"><span className="spinner" />Loading…</div>;
  }

  const showOos = outOfStock.length > 0;

  return (
    <>
      {renderTiles(inStock)}

      {showOos && (
        <div className="panel" style={{ marginTop: 20 }}>
          <div className="panel-header">
            <h2>Out of Stock</h2>
            <span className="badge">{outOfStock.length}</span>
          </div>
          {renderList(outOfStock)}
        </div>
      )}

      <div className="panel" style={{ marginTop: 20 }}>
        <div className="panel-header">
          <h2>Likely Depleted / Unknown</h2>
          <span className="badge">{depleted.length + unknown.length}</span>
        </div>
        {depleted.length + unknown.length === 0 ? (
          <div className="empty">No depleted or untracked items.</div>
        ) : (
          renderList([...depleted, ...unknown])
        )}
      </div>
    </>
  );
}
