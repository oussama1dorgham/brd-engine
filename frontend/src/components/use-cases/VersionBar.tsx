import { useCallback, useEffect, useState } from "react";
import {
  getSuiteDiff, getSuiteStatus, labelSuiteVersion, publishSuite, restoreSuiteVersion,
  type SuiteChange, type SuiteDiff, type SuiteDiffItem, type SuiteStatus, type SuiteSummary,
} from "../../lib/api";
import { DiffField, FIELD_LABEL, ORDER, type CaseField as ProposalField } from "./CaseDiff";

const CHANGE_LABEL: Record<SuiteChange, string> = {
  added: "Added", removed: "Removed", modified: "Modified", requirement_changed: "Requirement changed",
  moved: "Moved", reordered: "Reordered",
};
const CHANGE_ORDER: SuiteChange[] = ["modified", "requirement_changed", "added", "removed", "moved", "reordered"];

export const pendingChanges = (s?: SuiteSummary | null) =>
  s ? s.added + s.removed + s.modified + s.requirement_changed + s.moved + s.reordered : 0;

const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

// The version bar: which published version the suite is on, what the draft (the live cases)
// changes against it, and the version history. Publishing freezes the draft as the next
// immutable version — only after the user has seen exactly what changes.
export default function VersionBar({ project, refreshKey, reqLabel, toast, askPrompt, confirm, onDraftChanged }: {
  project: string;
  refreshKey: unknown;                      // anything that changes when the cases change
  reqLabel: (cid: number) => string;
  toast: (m: string) => void;
  askPrompt: (title: string, def: string, okLabel: string, placeholder?: string) => Promise<string | null>;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;
  onDraftChanged: () => Promise<void> | void;   // the draft was replaced (restore) → reload the tree
}) {
  const [st, setSt] = useState<SuiteStatus | null>(null);
  const [menu, setMenu] = useState(false);
  const [review, setReview] = useState<{ base: number | null; target: number | "draft" } | null>(null);

  const load = useCallback(() => {
    getSuiteStatus(project).then((s) => { if (!s.error) setSt(s); }).catch(() => {});
  }, [project]);
  useEffect(() => {
    const t = setTimeout(load, 400);        // debounced: edits arrive in bursts
    return () => clearTimeout(t);
  }, [load, refreshKey]);

  if (!st) return null;
  const n = pendingChanges(st.draft);
  const latest = st.latest;

  // git-revert style: the draft becomes that version; nothing published changes until you publish
  const restore = async (id: number, number: number, discard: boolean) => {
    const pending = pendingChanges(st?.draft);
    const ok = await confirm(discard
      ? `Discard the ${pending} unpublished change${pending === 1 ? "" : "s"} and return the draft to v${number}? `
        + "Each case's edits stay in its history (and deleted cases in Trash)."
      : `Replace the draft with v${number}'s use cases? Your current draft`
        + (pending ? ` (including ${pending} unpublished change${pending === 1 ? "" : "s"})` : "")
        + ` is replaced — each case's history keeps it. Publish afterwards to make v${number}'s cases the newest version.`,
      discard ? "Discard changes" : `Restore v${number}`);
    if (!ok) return;
    let r: Awaited<ReturnType<typeof restoreSuiteVersion>>;
    try { r = await restoreSuiteVersion(project, id); } catch (e) { toast(e instanceof Error ? e.message : "Something went wrong — try again."); return; }
    if (r.error) { toast(r.error); return; }
    toast(discard ? `Draft is back to v${number}`
      : `Draft restored from v${number} — review and publish it as a new version`
        + (r.requirements_changed_since ? ` (${r.requirements_changed_since} requirement${r.requirements_changed_since === 1 ? "" : "s"} changed since — Update to refresh)` : ""));
    await onDraftChanged();
    load();
  };

  const rename = async (id: number, cur: string | null) => {
    const label = await askPrompt("Version label", cur || "", "Save", "e.g. Sprint 12 baseline");
    if (label == null) return;
    try {
      const r = await labelSuiteVersion(project, id, label);
      if (r.error) toast(r.error); else load();
    } catch (e) { toast(e instanceof Error ? e.message : "Something went wrong — try again."); }
  };

  return (
    <>
      <div className="uc-stdrop">
        <button className="uc-tbbtn uc-verpill" onClick={() => setMenu((o) => !o)} aria-expanded={menu}
                title="Published versions of these use cases">
          ◆ {latest ? `v${latest.number}` : "Unpublished"}
          {latest?.label ? <em className="uc-verlabel" dir="auto">{latest.label}</em> : null} ▾
        </button>
        {menu && (
          <>
            <div className="uc-stdrop-backdrop" onClick={() => setMenu(false)} />
            <div className="uc-stdrop-menu uc-vermenu">
              {latest && n > 0 && (
                <button className="uc-cyc-item act" onClick={() => { setMenu(false); restore(latest.id, latest.number, true); }}>
                  ↺ Discard unpublished changes <em>return the draft to v{latest.number}</em>
                </button>
              )}
              <div className="uc-cyc-sep">Versions</div>
              {latest && st.brd_revision_no && latest.brd_revision_no && st.brd_revision_no !== latest.brd_revision_no && (
                <div className="uc-warn uc-verstale">
                  The BRD changed since v{latest.number} (built on revision {latest.brd_revision_no}, now {st.brd_revision_no}).
                  Update the use cases, review, then publish a new version.
                </div>
              )}
              {st.versions.length === 0 && <div className="settings-hint">Nothing published yet — publish to freeze v1.</div>}
              {st.versions.map((v, i) => {
                const prev = st.versions[i + 1];
                return (
                  <div key={v.id} className="uc-veritem">
                    <div className="uc-verrow">
                      <b>v{v.number}</b>
                      {v.label && <span className="uc-verlabel" dir="auto">{v.label}</span>}
                      <span className="uc-tsp" />
                      <span className="settings-hint">{v.counts.cases ?? 0} cases · {v.runs} run{v.runs === 1 ? "" : "s"}</span>
                    </div>
                    <div className="settings-hint">
                      {when(v.created_at)}{v.created_by ? ` · ${v.created_by}` : ""}
                      {v.source === "baseline" ? " · baseline" : ""}
                      {v.brd_revision_no ? ` · BRD rev ${v.brd_revision_no}` : ""}
                    </div>
                    <div className="uc-veracts">
                      {prev && <button className="linkbtn" onClick={() => { setMenu(false); setReview({ base: prev.id, target: v.id }); }}>
                        Compare with v{prev.number}</button>}
                      <button className="linkbtn" onClick={() => { setMenu(false); setReview({ base: v.id, target: "draft" }); }}>
                        Compare with draft</button>
                      <button className="linkbtn" onClick={() => { setMenu(false); rename(v.id, v.label); }}>Rename</button>
                      {v.id !== latest?.id && (
                        <button className="linkbtn" onClick={() => { setMenu(false); restore(v.id, v.number, false); }}>
                          Restore as draft</button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
      {(n > 0 || !latest) && (
        <button className="uc-tbbtn attn" onClick={() => setReview({ base: latest?.id ?? null, target: "draft" })}
                title="Review what changed since the last published version, then publish it as a new version">
          {latest ? `● ${n} unpublished change${n === 1 ? "" : "s"}` : "◆ Publish v1"}
        </button>
      )}
      {review && (
        <VersionReview project={project} base={review.base} target={review.target} reqLabel={reqLabel}
                       toast={toast} onClose={() => setReview(null)} onPublished={() => { setReview(null); load(); }} />
      )}
    </>
  );
}

function VersionReview({ project, base, target, reqLabel, toast, onClose, onPublished }: {
  project: string; base: number | null; target: number | "draft";
  reqLabel: (cid: number) => string; toast: (m: string) => void;
  onClose: () => void; onPublished: () => void;
}) {
  const [d, setD] = useState<SuiteDiff | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setD(null);
    getSuiteDiff(project, base, target).then(setD).catch(() => toast("Could not load the comparison"));
  }, [project, base, target, toast]);
  useEffect(load, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const publish = async () => {
    if (!d?.fingerprint) return;
    setBusy(true);
    let r: Awaited<ReturnType<typeof publishSuite>>;
    try { r = await publishSuite(project, label, d.fingerprint); }
    catch (e) { toast(e instanceof Error ? e.message : "Something went wrong — try again."); return; }
    finally { setBusy(false); }
    if (r.error) { toast(r.error); load(); return; }           // stale → show the fresh diff
    toast(r.created ? `Published v${r.version?.number}` : "Nothing changed — no new version");
    onPublished();
  };

  const side = (x: SuiteDiff["base"] | SuiteDiff["target"] | undefined) =>
    x === "draft" ? "Draft" : x ? `v${x.number}` : "Nothing";
  const toggle = (uid: number) => setOpen((s) => { const n = new Set(s); n.has(uid) ? n.delete(uid) : n.add(uid); return n; });
  const isDraft = target === "draft";
  const groups = d ? CHANGE_ORDER.map((c) => [c, d.items.filter((i) => i.change === c)] as const).filter(([, xs]) => xs.length) : [];

  return (
    <div className="uc-trash-scrim" onClick={onClose}>
      <div className="uc-trash uc-report" onClick={(e) => e.stopPropagation()}>
        <div className="uc-trash-head">
          <b>{d ? `${side(d.base)} → ${side(d.target)}` : "Comparing…"}</b>
          <span className="settings-hint">
            {isDraft ? "What publishing would freeze as the next version." : "What changed between these versions."}
          </span>
          <span className="uc-tsp" />
          <button className="linkbtn" onClick={onClose}>Close</button>
        </div>
        <div className="uc-trash-body uc-report-body">
          {!d && <div className="settings-hint">Loading…</div>}
          {d && (
            <div className="uc-ver-sum">
              {CHANGE_ORDER.filter((c) => d.summary[c]).map((c) => (
                <span key={c} className={"uc-chip ch-" + c}>{CHANGE_LABEL[c]} <span className="n">{d.summary[c]}</span></span>
              ))}
              {d.summary.retest > 0 && <span className="uc-chip uc-chip-retest">⚠ Needs retest <span className="n">{d.summary.retest}</span></span>}
              <span className="settings-hint">{d.summary.unchanged} unchanged</span>
            </div>
          )}
          {d && !d.summary.has_changes && <div className="settings-hint">No differences ✓</div>}
          {groups.map(([c, xs]) => (
            <div key={c} className="uc-ver-group">
              <div className="uc-cyc-sep">{CHANGE_LABEL[c]} · {xs.length}</div>
              {xs.map((i) => <DiffItem key={i.uid} i={i} open={open.has(i.uid)} onToggle={() => toggle(i.uid)} reqLabel={reqLabel} />)}
            </div>
          ))}
        </div>
        {isDraft && d && (d.summary.has_changes || !d.base) && (
          <div className="uc-ver-publish">
            <input className="uc-compose-in" dir="auto" placeholder="Label (optional) — e.g. After payment changes"
                   value={label} maxLength={200} onChange={(e) => setLabel(e.target.value)} />
            <button className="uc-compose-send on" disabled={busy} onClick={publish}>
              {busy ? "Publishing…" : `Publish as v${(d.base?.number ?? 0) + 1}`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function DiffItem({ i, open, onToggle, reqLabel }: {
  i: SuiteDiffItem; open: boolean; onToggle: () => void; reqLabel: (cid: number) => string;
}) {
  const detail = i.change === "modified" || i.change === "requirement_changed" || i.change === "moved";
  const fields = (i.fields.filter((f) => (ORDER as string[]).includes(f)) as ProposalField[])
    .sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
  return (
    <div className={"uc-ver-item ch-" + i.change}>
      <div className="uc-prop-head" onClick={detail ? onToggle : undefined} role={detail ? "button" : undefined}>
        {detail && <span className="uc-caret">{open ? "▾" : "▸"}</span>}
        <span className="uc-id">{i.uc_id}</span>
        <b className="uc-prop-title" dir="auto">{i.title}</b>
        {i.case_type && i.case_type !== "functional" && <span className={"uc-ctag ct-" + i.case_type}>{i.case_type}</span>}
        <span className="uc-tsp" />
        {i.fields.length > 0 && <span className="settings-hint">{i.fields.map((f) => FIELD_LABEL[f as ProposalField] || f).join(", ")}</span>}
        {i.moved && i.change !== "moved" && <span className="settings-hint">· moved</span>}
        {i.retest && <span className="uc-chip uc-chip-retest">⚠ retest</span>}
      </div>
      {open && (
        <div className="uc-diff" dir="auto">
          {i.change === "modified" && i.before && i.after && (
            <>
              <div className="uc-diff-cols uc-diff-hdr"><span>Before</span><span>After</span></div>
              {fields.map((f) => (
                <div key={f} className="uc-diff-field">
                  <div className="uc-flabel">{FIELD_LABEL[f]}</div>
                  <DiffField field={f} before={i.before![f]} after={i.after![f]} label={reqLabel} />
                </div>
              ))}
            </>
          )}
          {i.requirements.map((r) => (
            <div key={r.chunk_id} className="uc-diff-field">
              <div className="uc-flabel">Requirement {reqLabel(r.chunk_id)} changed</div>
              <div className="uc-diff-cols">
                <div className="uc-diff-old"><span className="del">{r.before ?? "—"}</span></div>
                <div className="uc-diff-new"><span className="add">{r.after ?? "—"}</span></div>
              </div>
            </div>
          ))}
          {i.moved && (
            <div className="uc-diff-field">
              <div className="uc-flabel">Folder</div>
              <div className="uc-diff-cols">
                <div className="uc-diff-old"><span className="del">{(i.folder_before || []).join(" › ") || "—"}</span></div>
                <div className="uc-diff-new"><span className="add">{(i.folder_after || []).join(" › ") || "—"}</span></div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
