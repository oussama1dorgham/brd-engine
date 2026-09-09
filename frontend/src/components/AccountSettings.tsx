import { useEffect, useState } from "react";
import {
  addLlmKey, changePassword, createServiceAccount, deleteLlmKey, deleteServiceAccount,
  getLlmKeys, getLlmProviders, getProjects, getServiceAccounts, grantProject, issueToken,
  revokeGrant, revokeToken, setActiveKey, setServiceAccountDisabled,
  type LlmKey, type ProviderMeta, type ServiceAccount,
} from "../lib/api";
import type { Project } from "../types";

export default function AccountSettings({ email, toast, onLlmChange, onKeySaved, onManageModels }: {
  email: string;
  toast: (m: string) => void;
  onLlmChange?: () => void;      // refresh the app/chat picker after any key change
  onKeySaved?: () => void;       // after a key validates → refresh + prompt model selection
  onManageModels?: () => void;   // open the "choose your models" modal (for the active key)
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

  // --- custom LLM (bring-your-own-key), multiple keys ---
  const [available, setAvailable] = useState(true);
  const [keys, setKeys] = useState<LlmKey[]>([]);
  const [providers, setProviders] = useState<ProviderMeta[]>([]);
  const [provider, setProvider] = useState("openai");
  const [baseUrl, setBaseUrl] = useState("https://openrouter.ai/api/v1");
  const [apiKey, setApiKey] = useState("");
  const [label, setLabel] = useState("");
  const [adding, setAdding] = useState(false);
  const [llmErr, setLlmErr] = useState<string | null>(null);
  const [llmBusy, setLlmBusy] = useState(false);

  const refreshKeys = () => getLlmKeys()
    .then((d) => { setAvailable(d.available); setKeys(d.keys || []); })
    .catch(() => setKeys([]));

  useEffect(() => { refreshKeys(); }, []);
  useEffect(() => { getLlmProviders().then((d) => setProviders(d.providers)).catch(() => setProviders([])); }, []);

  const providerMeta = providers.find((p) => p.key === provider);
  const needsBaseUrl = providerMeta ? providerMeta.needs_base_url : provider === "openai";
  const providerLabel = (key: string) => providers.find((p) => p.key === key)?.label ?? key;

  const chooseProvider = (key: string) => {
    setProvider(key);
    setBaseUrl(providers.find((p) => p.key === key)?.default_base_url ?? "");
  };

  const addKey = async (e: React.FormEvent) => {
    e.preventDefault();
    setLlmErr(null);
    setLlmBusy(true);
    try {
      const res = await addLlmKey(provider, needsBaseUrl ? baseUrl.trim() : "", apiKey.trim(), label.trim());
      setApiKey(""); setLabel(""); setAdding(false);
      await refreshKeys();
      toast(`Key saved — ${res.models.length} models available ✓`);
      onKeySaved?.();     // refreshes the app + opens the models modal for the new (active) key
    } catch (ex) {
      setLlmErr((ex as Error).message);
    }
    setLlmBusy(false);
  };

  const makeActive = async (id: number) => {
    await setActiveKey(id);
    await refreshKeys();
    onLlmChange?.();
  };

  const manageModels = async (id: number, isActive: boolean) => {
    if (!isActive) { await setActiveKey(id); await refreshKeys(); onLlmChange?.(); }
    onManageModels?.();   // the modal edits the active key
  };

  const removeKey = async (id: number) => {
    setLlmBusy(true);
    await deleteLlmKey(id);
    await refreshKeys();
    onLlmChange?.();
    toast("API key removed");
    setLlmBusy(false);
  };

  // --- API access: service accounts + scoped tokens (external services) ---
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [newName, setNewName] = useState("");
  const [saBusy, setSaBusy] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);   // raw token, shown once
  const [issueFor, setIssueFor] = useState<number | null>(null);
  const [issScopes, setIssScopes] = useState<string[]>(["ask", "read"]);
  const [issExpiry, setIssExpiry] = useState("90");
  const [issRate, setIssRate] = useState("60");

  const refreshAccounts = () => getServiceAccounts().then(setAccounts).catch(() => setAccounts([]));
  useEffect(() => { refreshAccounts(); getProjects().then(setProjects).catch(() => setProjects([])); }, []);

  const createAcct = async () => {
    const n = newName.trim();
    if (!n) return;
    setSaBusy(true);
    const r = await createServiceAccount(n);
    if (r.error) toast(r.error);
    else { setNewName(""); await refreshAccounts(); toast("Service account created ✓"); }
    setSaBusy(false);
  };

  const toggleGrant = async (id: number, project: string, on: boolean) => {
    await (on ? grantProject(id, project) : revokeGrant(id, project));
    await refreshAccounts();
  };
  const toggleDisabled = async (a: ServiceAccount) => {
    await setServiceAccountDisabled(a.id, !a.disabled);
    await refreshAccounts();
  };
  const removeAcct = async (id: number) => {
    await deleteServiceAccount(id);
    await refreshAccounts();
    toast("Service account deleted");
  };
  const toggleScope = (s: string, on: boolean) =>
    setIssScopes((cur) => (on ? Array.from(new Set([...cur, s])) : cur.filter((x) => x !== s)));
  const doIssue = async (id: number) => {
    setSaBusy(true);
    const days = issExpiry.trim() ? (parseInt(issExpiry, 10) || null) : null;
    const r = await issueToken(id, issScopes.length ? issScopes : ["ask", "read"], parseInt(issRate, 10) || 60, days);
    if (r.error || !r.token) toast(r.error || "Could not issue token");
    else { setRevealed(r.token); setIssueFor(null); await refreshAccounts(); }
    setSaBusy(false);
  };
  const doRevokeToken = async (tid: number) => {
    await revokeToken(tid);
    await refreshAccounts();
    toast("Token revoked");
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
          <h3>Custom AI models</h3>
          {!available ? (
            <p className="settings-hint">Custom API keys aren't enabled on this server.</p>
          ) : (
            <>
              <p className="settings-hint">
                Add one or more keys from any supported provider, pick which is <b>active</b>, and choose the
                models each one shows in the chat. Keys are stored encrypted.
              </p>

              {keys.length > 0 && (
                <div className="keylist">
                  {keys.map((k) => (
                    <div key={k.id} className={"keyrow" + (k.is_active ? " active" : "")}>
                      <label className="keyradio" title={k.is_active ? "Active key" : "Make active"}>
                        <input type="radio" name="activekey" checked={k.is_active}
                               onChange={() => makeActive(k.id)} />
                      </label>
                      <div className="keymain">
                        <div className="keyline">
                          <b>{k.label || providerLabel(k.provider)}</b>
                          <span className="keytag">{providerLabel(k.provider)}</span>
                          {k.is_active && <span className="keyactive">active</span>}
                        </div>
                        <div className="keysub">{k.masked}{k.base_url ? ` · ${k.base_url}` : ""}</div>
                      </div>
                      <div className="keyacts">
                        <button className="linkbtn" onClick={() => manageModels(k.id, k.is_active)}>Choose models</button>
                        <button className="linkbtn danger" onClick={() => removeKey(k.id)} disabled={llmBusy}>Remove</button>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {adding || keys.length === 0 ? (
                <form onSubmit={addKey} className="keyform">
                  <label className="authfld">
                    <span>Provider</span>
                    <select className="settings-select" value={provider} onChange={(e) => chooseProvider(e.target.value)}>
                      {(providers.length ? providers : [{ key: "openai", label: "OpenAI-compatible", needs_base_url: true, default_base_url: "https://api.openai.com/v1" }])
                        .map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
                    </select>
                  </label>
                  {needsBaseUrl && (
                    <label className="authfld">
                      <span>Base URL</span>
                      <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.openai.com/v1" required />
                    </label>
                  )}
                  <label className="authfld">
                    <span>Label <em>(optional)</em></span>
                    <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Work OpenAI" maxLength={60} />
                  </label>
                  <label className="authfld">
                    <span>API key</span>
                    <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" placeholder="sk-…" required />
                  </label>
                  {llmErr && <div className="autherr">{llmErr}</div>}
                  <div className="settings-actions">
                    <button className="primary" type="submit" disabled={llmBusy}>{llmBusy ? "Validating…" : "Save & validate"}</button>
                    {keys.length > 0 && <button type="button" className="backbtn" onClick={() => { setAdding(false); setLlmErr(null); }}>Cancel</button>}
                  </div>
                </form>
              ) : (
                <button className="uploadbtn" onClick={() => { setAdding(true); setLlmErr(null); }}>
                  <span className="upl-label">+ Add another key</span>
                </button>
              )}
            </>
          )}
        </section>

        <section className="settings-card">
          <h3>API access (external services)</h3>
          <p className="settings-hint">
            Create a service account, grant it specific BRDs, and issue <b>read-only</b> tokens so another
            service can call the API (ask + read) on those BRDs. Tokens are shown <b>once</b>, are limited to
            the BRDs you grant, and can be revoked anytime. They never expose your password or provider keys.
          </p>

          {revealed && (
            <div className="tokenreveal">
              <div><b>Copy this token now — it won't be shown again.</b></div>
              <code className="tokenvalue">{revealed}</code>
              <div className="settings-actions">
                <button className="linkbtn" onClick={() => { navigator.clipboard?.writeText(revealed); toast("Copied ✓"); }}>Copy</button>
                <button className="linkbtn" onClick={() => setRevealed(null)}>Dismiss</button>
              </div>
            </div>
          )}

          {accounts.length > 0 && (
            <div className="keylist">
              {accounts.map((a) => (
                <div key={a.id} className={"keyrow" + (a.disabled ? "" : " active")}>
                  <div className="keymain">
                    <div className="keyline">
                      <b>{a.name}</b>
                      {a.disabled && <span className="keytag">disabled</span>}
                    </div>

                    <div className="keysub">Granted BRDs</div>
                    <div className="grantgrid">
                      {projects.length === 0 && <span className="settings-hint">No BRDs yet.</span>}
                      {projects.map((p) => (
                        <label key={p.project} className="grantitem">
                          <input type="checkbox" checked={a.grants.includes(p.project)}
                                 onChange={(e) => toggleGrant(a.id, p.project, e.target.checked)} />
                          {p.title || p.project}
                        </label>
                      ))}
                    </div>

                    {a.tokens.length > 0 && (
                      <div className="tokenlist">
                        {a.tokens.map((t) => (
                          <div key={t.id} className="tokenrow">
                            <span className="keysub">
                              {t.masked} · {t.scopes.join("+")}
                              {t.revoked_at ? " · revoked" : t.expires_at ? ` · expires ${t.expires_at.slice(0, 10)}` : " · no expiry"}
                            </span>
                            {!t.revoked_at && <button className="linkbtn danger" onClick={() => doRevokeToken(t.id)}>Revoke</button>}
                          </div>
                        ))}
                      </div>
                    )}

                    {issueFor === a.id ? (
                      <div className="issueform">
                        <label className="grantitem"><input type="checkbox" checked={issScopes.includes("ask")} onChange={(e) => toggleScope("ask", e.target.checked)} /> ask</label>
                        <label className="grantitem"><input type="checkbox" checked={issScopes.includes("read")} onChange={(e) => toggleScope("read", e.target.checked)} /> read</label>
                        <label className="issfld"><span>Expires (days)</span><input value={issExpiry} onChange={(e) => setIssExpiry(e.target.value)} placeholder="blank = never" /></label>
                        <label className="issfld"><span>Rate/min</span><input value={issRate} onChange={(e) => setIssRate(e.target.value)} /></label>
                        <button className="primary" disabled={saBusy} onClick={() => doIssue(a.id)}>{saBusy ? "…" : "Issue token"}</button>
                        <button className="backbtn" onClick={() => setIssueFor(null)}>Cancel</button>
                      </div>
                    ) : (
                      <div className="keyacts">
                        <button className="linkbtn" onClick={() => { setIssueFor(a.id); setIssScopes(["ask", "read"]); setIssExpiry("90"); setIssRate("60"); }}>Issue token</button>
                        <button className="linkbtn" onClick={() => toggleDisabled(a)}>{a.disabled ? "Enable" : "Disable"}</button>
                        <button className="linkbtn danger" onClick={() => removeAcct(a.id)}>Delete</button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="keyform" style={{ marginTop: 12 }}>
            <label className="authfld">
              <span>New service account name</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. Acme integration" maxLength={60} />
            </label>
            <button className="primary" disabled={saBusy || !newName.trim()} onClick={createAcct}>Create service account</button>
          </div>
        </section>
      </div>
    </div>
  );
}
