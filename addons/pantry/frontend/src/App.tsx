import { useEffect, useState } from "react";
import { api, type InventoryItem } from "./api";
import { PantryInventory, RecordPurchaseForm } from "./components/PantryInventory";
import { Analytics } from "./components/Analytics";
import { Admin } from "./components/Admin";
import { useItemControls } from "./components/itemControls";

type Tab = "pantry" | "analytics" | "admin";

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
      {controls.mergeSource && (
        <div className="merge-banner">
          <span>
            Merging <strong>{controls.mergeSource.name}</strong> — tap the item to keep
          </span>
          <button className="merge-cancel-btn" onClick={controls.cancelMerge}>
            Cancel
          </button>
        </div>
      )}
      {controls.mergeError && <div className="error">{controls.mergeError}</div>}

      <nav className="tab-bar">
        <button
          className={`tab-btn${activeTab === "pantry" ? " tab-btn--active" : ""}`}
          onClick={() => handleTabSwitch("pantry")}
        >
          Pantry
        </button>
        <button
          className={`tab-btn${activeTab === "analytics" ? " tab-btn--active" : ""}`}
          onClick={() => handleTabSwitch("analytics")}
        >
          Analytics
        </button>
        <button
          className={`tab-btn${activeTab === "admin" ? " tab-btn--active" : ""}`}
          onClick={() => handleTabSwitch("admin")}
        >
          Admin
        </button>
      </nav>

      {error && <div className="error">{error}</div>}

      {activeTab === "pantry" && (
        <>
          <RecordPurchaseForm onRefresh={refresh} />
          <PantryInventory items={inventoryItems} loading={loading} controls={controls} />
        </>
      )}

      {activeTab === "analytics" && <Analytics controls={controls} refreshKey={dataVersion} />}

      {activeTab === "admin" && <Admin onRefresh={refresh} />}
    </div>
  );
}
