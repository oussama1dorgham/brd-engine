import { useCallback, useEffect, useState } from "react";
import { getUsage, type KeyUsage, type UsageStatus } from "../../lib/api";

const money = (n: number | null | undefined) => (n == null ? "—" : `$${n < 0.01 && n > 0 ? n.toFixed(5) : n.toFixed(2)}`);

function Meter({ used, limit, label }: { used: number; limit: number; label: string }) {
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const tone = pct >= 90 ? " hot" : pct >= 70 ? " warm" : "";
  return (
    <div className="usage-meter" title={label}>
      <span className="usage-bar"><span className={"usage-fill" + tone} style={{ width: `${pct}%` }} /></span>
      <span className="usage-num">{label}</span>
    </div>
  );
}

function KeyBlock({ title, k, active }: { title: string; k: KeyUsage | null; active: boolean }) {
  if (!k) return null;
  return (
    <div className={"usage-block" + (active ? " active" : "")}>
      <div className="usage-head"><b>{title}</b>{active && <span className="usage-tag">in use</span>}</div>
      {!k.available ? <div className="settings-hint">{k.error}</div> : (
        <>
          {k.free_daily && k.free_daily.limit != null ? (
            <Meter used={k.free_daily.used ?? 0} limit={k.free_daily.limit}
                   label={`${k.free_daily.remaining ?? 0} of ${k.free_daily.limit} free-model requests left today`} />
          ) : <div className="settings-hint">No free-model allowance reported for this key.</div>}
          <div className="settings-hint">
            Credits used: {money(k.credits.used_today)} today · {money(k.credits.used_total)} in total
            {k.credits.limit != null ? ` · ${money(k.credits.remaining)} of ${money(k.credits.limit)} left` : ""}
            {k.is_free_tier ? " · free tier" : ""}
          </div>
        </>
      )}
    </div>
  );
}

// "How much is left": the model keys' daily free allowance (reported live by OpenRouter) and the
// embedding capacity this minute (Voyage reports nothing, so this is our own pacing ledger).
export default function UsagePanel() {
  const [u, setU] = useState<UsageStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback((refresh = false) => {
    setBusy(refresh);
    getUsage(refresh).then((d) => { if (!d.error) setU(d); }).catch(() => {}).finally(() => setBusy(false));
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(() => load(), 15000);
    return () => window.clearInterval(t);
  }, [load]);

  if (!u) return <div className="settings-hint">Checking usage…</div>;
  const e = u.embeddings;
  return (
    <div className="usage-panel">
      <div className="settings-hint">
        Your chats and generation run on {u.uses === "own" ? "your own model key" : "the server's shared key"}.
        <button className="linkbtn usage-refresh" disabled={busy} onClick={() => load(true)}>{busy ? "Refreshing…" : "Refresh"}</button>
      </div>
      <KeyBlock title="Your key" k={u.own_key} active={u.uses === "own"} />
      <KeyBlock title="Server key (shared)" k={u.system} active={u.uses === "system"} />
      {u.own_key?.free_daily && u.system?.free_daily
        && u.own_key.free_daily.used === u.system.free_daily.used && u.own_key.free_daily.limit === u.system.free_daily.limit && (
        <div className="settings-hint">
          Both keys show the same count: OpenRouter limits free-model requests per <b>account</b>, so keys on the same
          account share one daily allowance.
        </div>
      )}
      <div className="usage-block">
        <div className="usage-head"><b>Embeddings & search (Voyage)</b></div>
        <Meter used={e.rpm_used} limit={e.rpm_limit} label={`${Math.max(0, e.rpm_limit - e.rpm_used)} of ${e.rpm_limit} requests free this minute`} />
        <Meter used={e.tpm_used} limit={e.tpm_limit}
               label={`${Math.max(0, e.tpm_limit - e.tpm_used).toLocaleString()} of ${e.tpm_limit.toLocaleString()} tokens free this minute`} />
        <div className="settings-hint">
          {e.blocked_for > 0 ? `Paused ${e.blocked_for}s after a rate limit. ` : ""}
          {e.rpm_used > 0 ? `Window resets in ${Math.ceil(e.window_resets_in)}s. ` : ""}
          {e.reserved_for_chat ? `${e.reserved_for_chat} request/min is kept free for chat while a BRD is indexing. ` : ""}
          Voyage doesn't report remaining quota, so this is the app's own count ({e.scope}).
        </div>
      </div>
    </div>
  );
}
