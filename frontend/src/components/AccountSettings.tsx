import { useState } from "react";
import { changePassword } from "../lib/api";

export default function AccountSettings({ email, toast }: {
  email: string;
  toast: (m: string) => void;
}) {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (next.length < 8) { setErr("New password must be at least 8 characters."); return; }
    if (next !== confirm) { setErr("New passwords don't match."); return; }
    if (next === cur) { setErr("New password must differ from the current one."); return; }
    setBusy(true);
    try {
      await changePassword(cur, next);
      setCur(""); setNext(""); setConfirm("");
      toast("Password changed ✓");
    } catch (ex) {
      setErr((ex as Error).message);
    }
    setBusy(false);
  };

  return (
    <div className="settings">
      <div className="settings-inner">
        <h2>Account settings</h2>

        <section className="settings-card">
          <h3>Account</h3>
          <div className="settings-row">
            <span className="settings-label">Email</span>
            <span className="settings-value">{email}</span>
          </div>
        </section>

        <section className="settings-card">
          <h3>Change password</h3>
          <form onSubmit={submit}>
            <label className="authfld">
              <span>Current password</span>
              <input type="password" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" required />
            </label>
            <label className="authfld">
              <span>New password</span>
              <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" placeholder="at least 8 characters" required />
            </label>
            <label className="authfld">
              <span>Confirm new password</span>
              <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" required />
            </label>
            {err && <div className="autherr">{err}</div>}
            <button className="primary" type="submit" disabled={busy}>{busy ? "…" : "Update password"}</button>
          </form>
        </section>
      </div>
    </div>
  );
}
