import { useEffect, useState } from "react";
import { api, type InventoryItem } from "./api";
import { PantryInventory, RecordPurchaseForm } from "./components/PantryInventory";
import { Analytics } from "./components/Analytics";
import { Admin } from "./components/Admin";
import { useItemControls } from "./components/itemControls";

type Tab = "pantry" | "analytics" | "admin";

const TABS: { id: Tab; label: string }[] = [
  { id: "pantry", label: "Inventory" },
  { id: "analytics", label: "Analytics" },
  { id: "admin", label: "Admin" },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>("pantry");
  const [inventoryItems, setInventoryItems] = useState<InventoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Bumped after any per-item action so tabs with their own data (Analytics)
  // know to re-fetch.
  const [dataVersion, setDataVersion] = useState(0);

  const fetchInventory = () => {
    setLoading(true);
    api.getInventory()
      .then((inventory) => { setInventoryItems(inventory); setError(null); })
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  };

  // Refresh every tab's data after an action that can affect any of them
  // (merge, rename, mark out of stock, hide, record purchase…).
  const refresh = () => {
    fetchInventory();
    setDataVersion((v) => v + 1);
  };

  useEffect(() => { fetchInventory(); }, []);

  const controls = useItemControls(refresh);

  const handleTabSwitch = (tab: Tab) => {
    setActiveTab(tab);
    controls.resetTransient();
  };

  return (
    <div className="app">
      <header className="j-appbar">
        <img className="j-logo" src="/logo-64.png" alt="" />
        <a className="j-appbar-title" href="/">
          <small>Joshua</small>
          <strong>Pantry</strong>
        </a>
      </header>

      <nav className="j-tabs" role="tablist" aria-label="Pantry sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            className="j-tab"
            aria-selected={activeTab === t.id}
            onClick={() => handleTabSwitch(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <main className="j-content wide">
        {controls.mergeSource && (
          <div className="merge-banner" role="status">
            <span>
              Merging <strong>{controls.mergeSource.name}</strong>. Tap the item to keep.
            </span>
            <button className="j-btn sm" onClick={controls.cancelMerge}>
              Cancel
            </button>
          </div>
        )}
        {controls.mergeError && <div className="error">{controls.mergeError}</div>}

        {error && <div className="error">{error}</div>}

        {activeTab === "pantry" && (
          <PantryInventory items={inventoryItems} loading={loading} controls={controls} />
        )}

        {activeTab === "analytics" && <Analytics controls={controls} refreshKey={dataVersion} />}

        {activeTab === "admin" && <Admin onRefresh={refresh} />}
      </main>

      {activeTab === "pantry" && (
        <div className="j-bottom-bar">
          <RecordPurchaseForm onRefresh={refresh} />
        </div>
      )}
    </div>
  );
}
