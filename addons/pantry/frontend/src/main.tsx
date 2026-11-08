// Joshua Pantry frontend
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// The shared Joshua look first, then the pantry-only styles on top of it.
import "./joshua-ui.css";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
