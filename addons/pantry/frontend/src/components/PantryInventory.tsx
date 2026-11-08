import { Fragment, useRef, useState } from "react";
import { api, type InventoryItem } from "../api";
import { AliasEditor, Icon, ItemActions, RenameForm, type ItemControls } from "./itemControls";

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
    item.status === "in_stock" ? "ok" :
    item.status === "out_of_stock" ? "bad" :
    item.status === "likely_depleted" ? "warn" :
    "";

  const statusMeta =
    item.status === "in_stock" && item.estimated_depletion
      ? daysUntil(item.estimated_depletion)
      : null;

  const purchasedMeta = item.last_purchased_at
    ? `bought ${fmtDate(item.last_purchased_at)}`
    : "never purchased";

  return (
    <li className={`item${isMergeSource ? " item--merge-source" : ""}${mergeMode && !isMergeSource ? " item--merge-target" : ""}`}>
      <div className="item-row">
        <span className={`j-dot ${dotClass}`} />
        <div className="j-list-main">
          <button type="button" className="item-name" onClick={() => onNameTap(item)} aria-expanded={expanded}>
            {item.name}
            {item.aliases.length > 0 && <span className="j-badge plain aka-tag">aka {item.aliases.length}</span>}
          </button>
          <span className="j-list-sub">
            {statusMeta && <span className="item-left">{statusMeta}</span>}
            {statusMeta && " · "}
            {purchasedMeta}
          </span>
        </div>
        {!mergeMode && (
          <div className="item-actions">
            <ItemActions
              item={item}
              onRefresh={refresh}
              onMergeStart={() => onMergeStart(item)}
              onRenameStart={() => onRenameStart(item)}
              renaming={renaming}
            />
          </div>
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
      <label className={`j-field${state === "error" ? " invalid" : ""}`}>
        <Icon name="plus" />
        <input
          ref={inputRef}
          type="text"
          placeholder="Record a purchase…"
          aria-label="Record a purchase"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={state === "loading"}
        />
      </label>
      <button
        type="submit"
        className={`j-btn primary record-purchase-btn record-purchase-btn--${state}`}
        disabled={!name.trim() || state !== "idle"}
      >
        {state === "loading" ? <span className="j-spinner" /> : state === "done" ? <Icon name="check" /> : state === "error" ? "Retry" : "Add"}
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
      <ul className="j-list item-list">
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
      return <div className="j-empty">No items in stock.</div>;
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
              <section key={cat || "__other"} className="tile-card">
                <h2 className="j-label">
                  {cat || "Other"} <span className="j-count">{groups.get(cat)!.length}</span>
                </h2>
                <ul className="j-list item-list">
                  {groups.get(cat)!.map(makeRow)}
                </ul>
              </section>
            ))}
          </div>
        ))}
      </div>
    );
  };

  if (loading) {
    return <div className="j-empty loading"><span className="j-spinner" />Loading…</div>;
  }

  const showOos = outOfStock.length > 0;

  return (
    <>
      <div className="j-stats">
        <div className="j-stat ok"><b>{inStock.length}</b><span>In stock</span></div>
        <div className="j-stat warn"><b>{depleted.length}</b><span>Likely depleted</span></div>
        <div className="j-stat bad"><b>{outOfStock.length}</b><span>Out of stock</span></div>
        <div className="j-stat"><b>{unknown.length}</b><span>Unknown</span></div>
      </div>

      {renderTiles(inStock)}

      {showOos && (
        <section className="stock-section">
          <h2 className="j-label">
            Out of stock <span className="j-count">{outOfStock.length}</span>
          </h2>
          {renderList(outOfStock)}
        </section>
      )}

      <section className="stock-section">
        <h2 className="j-label">
          Likely depleted / unknown <span className="j-count">{depleted.length + unknown.length}</span>
        </h2>
        {depleted.length + unknown.length === 0 ? (
          <div className="j-list j-empty">No depleted or untracked items.</div>
        ) : (
          renderList([...depleted, ...unknown])
        )}
      </section>
    </>
  );
}
