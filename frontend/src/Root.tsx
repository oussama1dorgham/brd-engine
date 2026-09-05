import { useEffect, useState } from "react";
import { getMe, logout, type AuthUser } from "./lib/api";
import App from "./App";
import AuthPage from "./components/AuthPage";

export default function Root() {
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined); // undefined = still checking
  // Legacy verify-link result (kept; the active flow verifies via an OTP code).
  const [verified] = useState<string | null>(() => new URLSearchParams(window.location.search).get("verified"));

  useEffect(() => {
    // strip auth query params from the address bar
    if (window.location.search) window.history.replaceState({}, "", window.location.pathname);
  }, []);
  useEffect(() => { getMe().then(setUser); }, []);

  if (user === undefined) return <div className="bootscreen" />;
  if (!user) {
    const notice =
      verified === "1" ? "Email verified — please log in." :
      verified === "0" ? "That verification link is invalid or expired." : null;
    return <AuthPage onAuthed={setUser} initialNotice={notice} />;
  }
  return <App user={user} onLogout={async () => { await logout(); setUser(null); }} verifiedNotice={verified === "1"} />;
}
