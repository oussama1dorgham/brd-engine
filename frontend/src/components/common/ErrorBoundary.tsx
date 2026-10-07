import { Component, type ErrorInfo, type ReactNode } from "react";

// Last-resort guard around the whole app: a render-time exception anywhere below (e.g. a
// component that dereferences a field from a failed fetch) used to white-screen the tab with
// no way back. This catches it, logs it for diagnosis, and offers a reload instead of a blank
// page. It can't catch errors from event handlers or async code (React doesn't route those
// here) — those are handled at the call site (toast + fallback state).
export default class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("Unhandled error in the app tree:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
          height: "100vh", gap: 14, textAlign: "center", padding: 24, fontFamily: "inherit",
        }}>
          <div style={{ fontSize: 15, opacity: 0.85 }}>
            Something went wrong and this page can't continue. Your data is safe — nothing was lost.
          </div>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: "8px 18px", borderRadius: 8, border: "1px solid currentColor",
              background: "transparent", cursor: "pointer", fontSize: 14,
            }}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
