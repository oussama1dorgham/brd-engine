import { useEffect, useMemo, useState } from "react";
import {
  applyRevision, discardRevision, getRevision, recompareRevision,
  type BrdRevision, type RevisionChange, type RevisionItem,
} from "../../lib/api";

const CHANGE_LABEL: Record<RevisionChange, string> = {
  modified: "Modified", added: "Added", removed: "Removed", same: "Unchanged",
};
const ORDER: RevisionChange[] = ["modified", "added", "removed", "same"];

// The server's RevisionError message for "this revision was already applied / discarded /
// superseded" (e.g. another tab resolved it first) — not a dedicated field, just its text.
const ALREADY_RESOLVED_RE = /already (applied|discarded|superseded)/i;

// Review a NEW VERSION of a BRD before anything is replaced. Every change is opt-out:
// untick a modification to keep the current text, an addition to leave it out, a removal to
// KEEP the requirement. Removals are archived (never deleted): citations and use cases keep
// resolving and a Versions restore brings them back under the same id.
export default function RevisionReview({ project, title, onClose, onDone, toast, confirm }: {
  project: string;
  title: string;
  onClose: () => void;
  onDone: () => void;
  toast: (m: string) => void;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;
}) {
  const [rev, setRev] = useState<BrdRevision | null | undefined>(undefined);   // undefined = loading
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filter, setFilter] = useState<RevisionChange | "changes">("changes");
  const [off, setOff] = useState<Set<number>>(new Set());     // item ids the user opted out of
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setLoadError(null);
    setRev(undefined);
    try {
      const d = await getRevision(project);
      if (d.error) { setLoadError(d.error); setRev(null); return; }
      setRev(d.revision ?? null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Couldn't load the comparison — check your connection and try again.");
      setRev(null);
    }
  };
  useEffect(() => { load(); }, [project]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const items = rev?.items || [];
  const shown = useMemo(() => items.filter((i) => (filter === "changes" ? i.change !== "same" : i.change === filter)), [items, filter]);
  const changes = items.filter((i) => i.change !== "same");
  const applied = changes.filter((i) => !off.has(i.id)).length;
  const removals = items.filter((i) => i.change === "removed" && !off.has(i.id));
  const riskyRemovals = removals.filter((i) => i.use_cases > 0);

  const toggle = (id: number) => setOff((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  // `already resolved` (another tab applied/discarded/superseded this revision first): nothing
  // left to show here — surface it and close/refresh rather than leaving a stale comparison up.
  const handleAlreadyResolved = (msg: string) => { toast(msg); onDone(); };

  const doApply = async (confirmReplace: boolean): Promise<void> => {
    if (!rev) return;
    const keep = items.filter((i) => i.change === "removed" && off.has(i.id)).map((i) => i.id);
    const skip = items.filter((i) => (i.change === "modified" || i.change === "added") && off.has(i.id)).map((i) => i.id);
    const r = await applyRevision(rev.id, keep, skip, confirmReplace);
    if (r.stale) { toast("The BRD was edited after this comparison — re-comparing…"); await recompare(); return; }
    if (r.confirm_required) {
      const s = r.summary;
      const extra = s ? ` (${s.matched}/${s.live} current requirements matched — ${Math.round((s.matched_ratio || 0) * 100)}%)` : "";
      if (await confirm((r.error || "This file barely matches the current BRD — applying it would replace most or all of it.") + extra, "Replace everything")) {
        await doApply(true);
      }
      return;
    }
    if (r.error) {
      if (ALREADY_RESOLVED_RE.test(r.error)) { handleAlreadyResolved(r.error); return; }
      toast(r.error);
      return;
    }
    toast(`New version applied — ${r.updated || 0} updated, ${r.added || 0} added, ${r.archived || 0} archived`);
    onDone();
  };
  const apply = async () => {
    if (!rev) return;
    if (riskyRemovals.length && !(await confirm(
      `${riskyRemovals.length} requirement${riskyRemovals.length > 1 ? "s" : ""} you are removing ` +
      `${riskyRemovals.length > 1 ? "are" : "is"} cited by use cases. They are archived (not deleted): the use cases ` +
      `keep their link and can be reviewed next. Apply the new version?`, "Apply new version"))) return;
    setBusy(true);
    try { await doApply(false); }
    catch (e) { toast(e instanceof Error ? e.message : "Could not apply the new version."); }
    finally { setBusy(false); }
  };
  const discard = async () => {
    if (!rev || !(await confirm("Discard this new version? The current BRD stays exactly as it is.", "Discard version"))) return;
    setBusy(true);
    try {
      const r = await discardRevision(rev.id);
      if (r.error) {
        if (ALREADY_RESOLVED_RE.test(r.error)) { handleAlreadyResolved(r.error); return; }
        toast(r.error);
        return;
      }
      toast("New version discarded");
      onDone();
    } catch (e) { toast(e instanceof Error ? e.message : "Could not discard the new version."); }
    finally { setBusy(false); }
  };
  const recompare = async () => {
    if (!rev) return;
    setBusy(true);
    try {
      const r = await recompareRevision(rev.id);
      if (r.error) {
        if (ALREADY_RESOLVED_RE.test(r.error)) { handleAlreadyResolved(r.error); return; }
        toast(r.error);
        return;
      }
      setOff(new Set());
      await load();
    } catch (e) { toast(e instanceof Error ? e.message : "Could not re-compare the new version."); }
    finally { setBusy(false); }
  };

  const Item = ({ it }: { it: RevisionItem }) => {
    const on = !off.has(it.id);
    const label = it.req_id || it.section_path || it.section || `Requirement ${(it.ordinal ?? 0) + 1}`;
    const choice = it.change === "modified" ? "Apply this change" : it.change === "added" ? "Add this requirement"
      : it.change === "removed" ? "Remove (archive) this requirement" : null;
    return (
      <div className={"rv-item c-" + it.change + (on ? "" : " off")}>
        <div className="rv-item-head">
          <span className={"rv-chg c-" + it.change}>{CHANGE_LABEL[it.change]}</span>
          <b dir="auto">{label}</b>
          {it.section_path && it.req_id && <span className="rv-path" dir="auto">{it.section_path}</span>}
          {it.page != null && <span className="rv-path">p. {it.page}</span>}
          {(it.use_cases > 0) && (
            <span className={"rv-impact" + (it.tested_use_cases ? " tested" : "")}
                  title="Use cases whose source requirements include this one">
              cited by {it.use_cases} use case{it.use_cases > 1 ? "s" : ""}{it.tested_use_cases ? ` · ${it.tested_use_cases} tested` : ""}
            </span>
          )}
          <span className="uc-tsp" />
          {choice && (
            <label className="rv-toggle">
              <input type="checkbox" checked={on} onChange={() => toggle(it.id)} /> {choice}
            </label>
          )}
        </div>
        {it.change === "modified" ? (
          <div className="uc-diff-cols">
            <div className="uc-diff-old" dir="auto"><span className="del">{it.old_text}</span></div>
            <div className="uc-diff-new" dir="auto"><span className="add">{it.text}</span></div>
          </div>
        ) : it.change === "removed" ? (
          <div className="uc-diff-old" dir="auto"><span className="del">{it.old_text}</span></div>
        ) : it.change === "added" ? (
          <div className="uc-diff-new" dir="auto"><span className="add">{it.text}</span></div>
        ) : (
          <div className="rv-same" dir="auto">{it.text}</div>
        )}
      </div>
    );
  };

  return (
    <div className="uc-trash-scrim rv-scrim" onClick={() => !busy && onClose()}>
      <div className="uc-trash uc-report" onClick={(e) => e.stopPropagation()}>
        <div className="uc-trash-head">
          <b dir="auto">New version of “{title}”</b>
          {rev?.filename && <span className="settings-hint">{rev.filename} · {new Date(rev.created_at).toLocaleString()}</span>}
          <span className="uc-tsp" />
          <button className="linkbtn" disabled={busy} onClick={onClose}>Close</button>
        </div>
        <div className="uc-trash-body uc-report-body">
          {rev === undefined && !loadError && <div className="settings-hint">Loading the comparison…</div>}
          {loadError && (
            <div className="uc-warn rv-full">
              ⚠ {loadError}
              <button className="uc-compose-send on" onClick={load}>Retry</button>
            </div>
          )}
          {rev === null && !loadError && <div className="settings-hint">No new version is waiting for review.</div>}
          {rev && (
            <>
              <div className="rv-summary">
                {ORDER.map((k) => (
                  <button key={k} className={"uc-chip rv-chip c-" + k + (filter === k ? " on" : "")}
                          onClick={() => setFilter(filter === k ? "changes" : k)} disabled={!rev.summary[k]}>
                    <span className={"dot c-" + k} />{CHANGE_LABEL[k]} <span className="n">{rev.summary[k]}</span>
                  </button>
                ))}
              </div>
              {rev.needs_confirmation && (
                <div className="uc-warn rv-full">
                  ⚠ Only {Math.round((rev.replace_ratio || 0) * 100)}% of the current requirements match this file —
                  applying it would archive {rev.summary.removed} and add {rev.summary.added}.
                  Check that it is the right document and that its text reads correctly (open a few items below) before
                  applying; you'll be asked to confirm again.
                </div>
              )}
              {rev.stale && (
                <div className="uc-warn rv-stale">
                  ⚠ The BRD was edited after this comparison.
                  <button className="uc-compose-send on" disabled={busy} onClick={recompare}>Re-compare</button>
                </div>
              )}
              <p className="rv-note">
                Everything below is applied unless you untick it. Removed requirements are <b>archived, not deleted</b> —
                chat citations and use cases keep resolving, and you can restore them from <i>Versions</i>. A snapshot of the
                current BRD is saved before applying.
              </p>
              {changes.length === 0 && <div className="settings-hint">This file has no changes compared with the current BRD.</div>}
              <div className="rv-list">
                {shown.map((it) => <Item key={it.id} it={it} />)}
              </div>
            </>
          )}
        </div>
        {rev && (
          <div className="rv-foot">
            <button className="uc-ghostbtn" disabled={busy} onClick={discard}>Discard this version</button>
            <span className="uc-tsp" />
            <span className="settings-hint">{applied} of {changes.length} change{changes.length === 1 ? "" : "s"} selected</span>
            <button className="uc-compose-send on" disabled={busy || rev.stale} onClick={apply}>
              {busy ? "Working…" : `Apply new version`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
