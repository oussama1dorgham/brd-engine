import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import Root from "./Root";
import ErrorBoundary from "./components/common/ErrorBoundary";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <Root />
    </ErrorBoundary>
  </StrictMode>,
);
