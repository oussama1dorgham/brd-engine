import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getUseCaseAudit, getUseCaseDiff, restoreUseCase, TEST_STATUS_LABEL,
  type AuditCategory, type UcAuditEntry, type UcDiff,
} from "../../lib/api";

export const ago = (iso: string | null) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
};
const initials = (email: string | null) =>
  (email || "?").split("@")[0].split(/[._-]+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("") || "?";
const dayKey = (iso: string) => {
  const d = new Date(iso), t = new Date();
  const y = new Date(); y.setDate(t.getDate() - 1);
  if (d.toDateString() === t.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
};

const CATS: { k: AuditCategory; label: string; icon: string }[] = [
  { k: "test", label: "Test results", icon: "▶" },
  { k: "definition", label: "Definition", icon: "✎" },
  { k: "evidence", label: "Evidence", icon: "📎" },
  { k: "scope", label: "Scope", icon: "⊘" },
  { k: "cycle", label: "Cycles", icon: "🧪" },
  { k: "proposal", label: "Proposals", icon: "⟳" },
  { k: "version", label: "Versions", icon: "◆" },
];
const CAT_ICON = Object.fromEntries(CATS.map((c) => [c.k, c.icon])) as Record<AuditCategory, string>;
const fmtVal = (v: unknown): string =>
  Array.isArray(v) ? (v.length ? v.join(", ") : "—") : v == null || v === "" ? "—" : String(v);

const csvCell = (v: unknown) => {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

// The use case's own AUDIT LOG — every act on it, attributed and timestamped: definition
// edits / moves / restores (expandable diff + restore), verdicts with status before → after,
// amendments and undos, notes, evidence, scope, cycle planning, change proposals.
// Append-only on the server; see docs/USECASE_TESTING.md §14.
export default function UseCaseAudit({ uid, ucLabel, refreshKey, onRestored, toast, confirm }: {
  uid: number;
  ucLabel: string;                  // for the CSV file name
  refreshKey?: number;
  onRestored: () => void;
  toast: (m: string) => void;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;
}) {
  const [entries, setEntries] = useState<UcAuditEntry[] | null>(null);
  const [counts, setCounts] = useState<Partial<Record<AuditCategory, number>>>({});
  const [cat, setCat] = useState<AuditCategory | "all">("all");
  const [open, setOpen] = useState<number | null>(null);   // expanded definition entry
  const [diff, setDiff] = useState<UcDiff | null>(null);

  const load = useCallback(async () => {
    const d = await getUseCaseAudit(uid).catch(() => null);
    setEntries(d?.entries ?? []);
    setCounts(d?.counts ?? {});
  }, [uid]);
  useEffect(() => { setEntries(null); setOpen(null); setDiff(null); load(); }, [uid, load]);
  useEffect(() => { if (refreshKey) load(); }, [refreshKey, load]);

  const latestVersion = useMemo(
    () => Math.max(0, ...(entries || []).filter((e) => e.version_no).map((e) => e.version_no!)), [entries]);
  const shown = (entries || []).filter((e) => cat === "all" || e.category === cat);
  const groups: { day: string; items: UcAuditEntry[] }[] = [];
  for (const e of shown) {
    const d = dayKey(e.at);
    if (!groups.length || groups[groups.length - 1].day !== d) groups.push({ day: d, items: [] });
    groups[groups.length - 1].items.push(e);
  }
  const total = Object.values(counts).reduce((a, b) => a + (b || 0), 0);

  const toggleDiff = async (e: UcAuditEntry) => {
    if (open === e.id) { setOpen(null); return; }
    setOpen(e.id); setDiff(null);
    if (e.version_no) setDiff(await getUseCaseDiff(uid, e.version_no).catch(() => null));
  };
  const restore = async (v: number) => {
    if (!(await confirm(`Restore this use case's definition to v${v}? The current state stays in the audit log, so this is reversible.`,
      `Restore v${v}`))) return;
    const r = await restoreUseCase(uid, v);
    if (r.error) { toast(r.error); return; }
    toast("Restored ✓");
    await load();
    onRestored();
  };
  const exportCsv = () => {
    const header = ["When", "Who", "Source", "Category", "Action", "Summary", "Status before", "Status after",
      "Cycle", "Note", "Defect", "Definition version"];
    const rows = (entries || []).map((e) => [new Date(e.at).toLocaleString(), e.actor, e.source, e.category, e.action,
      e.summary, e.status_before ? TEST_STATUS_LABEL[e.status_before] : "", e.status_after ? TEST_STATUS_LABEL[e.status_after] : "",
      e.cycle_name, e.detail.note, e.detail.defect_ref, e.version_no]);
    const body = "﻿" + [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([body], { type: "text/csv;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `${ucLabel.replace(/[^\w.-]+/g, "_")}_audit.csv` });
    document.body.appendChild(a); a.click(); a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="uc-audit">
      <div className="uc-audit-bar">
        <div className="uc-filter">
          <button className={"uc-chip" + (cat === "all" ? " on" : "")} onClick={() => setCat("all")}>
            All <span className="n">{total}</span>
          </button>
          {CATS.filter((c) => counts[c.k]).map((c) => (
            <button key={c.k} className={"uc-chip" + (cat === c.k ? " on" : "")} onClick={() => setCat(c.k)}>
              {c.icon} {c.label} <span className="n">{counts[c.k]}</span>
            </button>
          ))}
        </div>
        <span className="uc-tsp" />
        <button className="uc-ghostbtn" disabled={!entries?.length} onClick={exportCsv}>⤓ Export CSV</button>
      </div>
      {!entries && <div className="settings-hint">Loading audit log…</div>}
      {entries && shown.length === 0 && <div className="settings-hint">Nothing recorded here yet.</div>}
      {groups.map((g) => (
        <div key={g.day} className="uc-audit-day">
          <div className="uc-audit-dayh">{g.day}</div>
          {g.items.map((e) => (
            <div key={e.id} className={"uc-aud c-" + e.category}>
              <span className="uc-aud-rail"><span className="uc-aud-ico" title={e.category}>{CAT_ICON[e.category] || "•"}</span></span>
              <div className="uc-aud-main">
                <div className="uc-aud-line">
                  <span className="uc-aud-sum" dir="auto">{e.summary || e.action}</span>
                  {e.status_before && e.status_after && e.status_before !== e.status_after && (
                    <span className="uc-aud-trans">
                      <span className={"uc-tstatus sm st-" + e.status_before}>{TEST_STATUS_LABEL[e.status_before]}</span>
                      <span className="uc-aud-arr">→</span>
                      <span className={"uc-tstatus sm st-" + e.status_after}>{TEST_STATUS_LABEL[e.status_after]}</span>
                    </span>
                  )}
                  {e.cycle_name && <span className="uc-tcycle" title="Test cycle">{e.cycle_name}</span>}
                  {e.version_no && (
                    <button className="uc-aud-ver" onClick={() => toggleDiff(e)} title="Show what changed in this version">
                      v{e.version_no} {open === e.id ? "▾" : "▸"}
                    </button>
                  )}
                </div>
                <div className="uc-aud-meta">
                  <span className="uc-avatar" aria-hidden>{e.source === "regeneration" ? "⚙" : initials(e.actor)}</span>
                  <b>{e.actor || (e.source === "regeneration" ? "Generator" : "System")}</b>
                  {e.source === "regeneration" && <span className="uc-aud-src">via regeneration</span>}
                  <span title={new Date(e.at).toLocaleString()}>{new Date(e.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · {ago(e.at)}</span>
                </div>
                {(e.detail.note || e.detail.defect_ref || e.detail.attachments || e.detail.note_before) && (
                  <div className="uc-aud-detail">
                    {e.detail.note_before && <div className="uc-aud-was" dir="auto">was: “{e.detail.note_before}”</div>}
                    {e.detail.note && <div dir="auto">“{e.detail.note}”</div>}
                    {e.detail.defect_ref && <span className="uc-defect">🐞 {e.detail.defect_ref}</span>}
                    {e.detail.attachments ? <span className="uc-aud-att">📎 {e.detail.attachments} attached</span> : null}
                  </div>
                )}
                {open === e.id && (
                  <div className="uc-hist-diff">
                    {!diff && <span className="settings-hint">Loading…</span>}
                    {diff && Object.keys(diff.fields).length === 0 && <span className="settings-hint">No field changes in this version.</span>}
                    {diff && Object.entries(diff.fields).map(([f, ch]) => (
                      <div key={f} className="uc-hist-field">
                        <span className="uc-hist-fname">{f.replace(/_/g, " ")}</span>
                        <span className="uc-diff-before">{fmtVal(ch.before)}</span>
                        <span className="uc-diff-arr">→</span>
                        <span className="uc-diff-after">{fmtVal(ch.after)}</span>
                      </div>
                    ))}
                    {e.version_no && e.version_no < latestVersion && (
                      <button className="uc-compose-send on uc-aud-restore" onClick={() => restore(e.version_no!)}>
                        ↺ Restore definition to v{e.version_no}
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
