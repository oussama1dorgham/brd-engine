import { useEffect, useState } from "react";
import {
  changePassword, deleteLlmKey, getLlmKey, setLlmKey, type LlmKeyMeta,
} from "../lib/api";

export default function AccountSettings({ email, toast, onLlmChange, onKeySaved, onManageModels }: {
  email: string;
  toast: (m: string) => void;
  onLlmChange?: () => void;
  onKeySaved?: () => void;      // after a key validates → refresh + prompt model selection
  onManageModels?: () => void;  // re-open the "choose your models" modal
}) {
  // --- change password ---
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

  // --- custom LLM (bring-your-own-key) ---
  const [llm, setLlm] = useState<LlmKeyMeta | null>(null);
  const [baseUrl, setBaseUrl] = useState("https://openrouter.ai/api/v1");
  const [apiKey, setApiKey] = useState("");
  const [llmErr, setLlmErr] = useState<string | null>(null);
  const [llmBusy, setLlmBusy] = useState(false);

  useEffect(() => { getLlmKey().then(setLlm).catch(() => setLlm(null)); }, []);

  const saveKey = async (e: React.FormEvent) => {
    e.preventDefault();
    setLlmErr(null);
    setLlmBusy(true);
    try {
      const res = await setLlmKey(baseUrl.trim(), apiKey.trim());
      setApiKey("");
      setLlm({ available: true, configured: true, base_url: res.base_url, masked: res.masked });
      toast(`Key saved — ${res.models.length} models available ✓`);
      onKeySaved?.();
    } catch (ex) {
      setLlmErr((ex as Error).message);
    }
    setLlmBusy(false);
  };

  const removeKey = async () => {
    setLlmBusy(true);
    await deleteLlmKey();
    setLlm({ available: true, configured: false });
    toast("API key removed");
    onLlmChange?.();
    setLlmBusy(false);
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

        <section className="settings-card">
          <h3>Custom AI model</h3>
          {llm && !llm.available ? (
            <p className="settings-hint">Custom API keys aren't enabled on this server.</p>
          ) : llm?.configured ? (
            <>
              <p className="settings-hint">
                Answers are generated with your own key. Pick the model from the selector at the top of a chat.
              </p>
              <div className="settings-row">
                <span className="settings-label">Provider</span>
                <span className="settings-value">{llm.base_url}</span>
              </div>
              <div className="settings-row">
                <span className="settings-label">API key</span>
                <span className="settings-value">{llm.masked}</span>
              </div>
              <div className="settings-actions">
                <button className="primary" onClick={() => onManageModels?.()}>Choose models</button>
                <button className="backbtn" onClick={removeKey} disabled={llmBusy}>Remove key</button>
              </div>
            </>
          ) : (
            <form onSubmit={saveKey}>
              <p className="settings-hint">
                Bring your own OpenAI-compatible key. We'll list the models it can use; it's stored encrypted.
              </p>
              <label className="authfld">
                <span>Base URL</span>
                <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://openrouter.ai/api/v1" required />
              </label>
              <label className="authfld">
                <span>API key</span>
                <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" placeholder="sk-…" required />
              </label>
              {llmErr && <div className="autherr">{llmErr}</div>}
              <button className="primary" type="submit" disabled={llmBusy}>{llmBusy ? "Validating…" : "Save & validate"}</button>
            </form>
          )}
        </section>
      </div>
    </div>
  );
}
