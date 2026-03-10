import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { init, disableVerticalSwipes } from "@telegram-apps/sdk-react";
import App from "./App";
import "./styles/global.css";

// Initialize Telegram Mini App SDK — gracefully handle running outside Telegram
try {
  init();
  disableVerticalSwipes();
} catch (e) {
  console.warn("Telegram Mini App SDK init failed (not in Telegram WebView?):", e);
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
