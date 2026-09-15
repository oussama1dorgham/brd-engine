import { useEffect, useState } from "react";
import { getUseCaseHistory, getUseCaseDiff, restoreUseCase, type UcVersion, type UcDiff } from "../lib/api";

const KIND_LABEL: Record<string, string> = {
  create: "Created", edit: "Edited", move: "Moved", delete: "Deleted",
  regenerate: "Regenerated", restore: "Restored",
};

const fmt = (v: unknown): string =>
  Array.isArray(v) ? (v.length ? v.join(", ") : "—") : v == null || v === "" ? "—" : String(v);

// Per-use-case version history + audit ("what changed") + restore (backup).
// Collapsed by default; loads on first open and when the selected card changes.
export default function UseCaseHistory({ uid, refreshKey, onRestored, toast, confirm }: {
  uid: number;
  refreshKey?: number;   // bumped by the parent on edit/move so an open panel refetches
  onRestored: () => void;
  toast: (m: string) => void;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;   // in-app modal, not window.confirm
}) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<UcVersion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [diff, setDiff] = useState<UcDiff | null>(null);

  const loadHistory = async () => {
    setLoading(true);
    const d = await getUseCaseHistory(uid).catch(() => null);
    setVersions(d?.versions ?? []);
    setLoading(false);
  };
  // reset when switching cards; refetch if the panel is open
  useEffect(() => {
    setVersions(null); setExpanded(null); setDiff(null);
    if (open) loadHistory();
  }, [uid]);  // eslint-disable-line react-hooks/exhaustive-deps

  // the card was edited/moved elsewhere: refetch if open, else drop the stale list so
  // the next open reloads (a new version was just recorded).
  useEffect(() => {
    if (!refreshKey) return;
    if (open) loadHistory(); else setVersions(null);
  }, [refreshKey]);  // eslint-disable-line react-hooks/exhaustive-deps

  const toggleOpen = () => {
    const n = !open;
    setOpen(n);
    if (n && !versions) loadHistory();
  };

  const toggleVersion = async (v: UcVersion) => {
    if (expanded === v.version_no) { setExpanded(null); return; }
    setExpanded(v.version_no); setDiff(null);
    const d = await getUseCaseDiff(uid, v.version_no).catch(() => null);
    setDiff(d);
  };

  const doRestore = async (v: UcVersion) => {
    const ok = await confirm(
      `Restore this use case to v${v.version_no}? The current state is kept in history, so you can undo this.`,
      `Restore v${v.version_no}`,
    );
    if (!ok) return;
    const r = await restoreUseCase(uid, v.version_no);
    if (r.error) { toast(r.error); return; }
    toast("Restored ✓");
    await loadHistory();
    onRestored();
  };

  return (
    <div className="uc-hist">
      <button className="uc-hist-head" onClick={toggleOpen}>
        <span className="uc-caret">{open ? "▾" : "▸"}</span>
        History{versions ? ` (${versions.length})` : ""}
      </button>
      {open && (
        <div className="uc-hist-body">
          {loading && <div className="settings-hint">Loading…</div>}
          {!loading && versions && versions.length === 0 && <div className="settings-hint">No history yet.</div>}
          {versions?.map((v, i) => (
            <div key={v.version_no} className="uc-hist-row">
              <div className="uc-hist-line">
                <button className="uc-hist-item" onClick={() => toggleVersion(v)} title="Show what changed">
                  <span className="uc-hist-ver">v{v.version_no}</span>
                  <span className={"uc-hist-kind k-" + v.change_kind}>{KIND_LABEL[v.change_kind] || v.change_kind}</span>
                  <span className="uc-hist-sum">{v.change_summary || ""}</span>
                  <span className="uc-hist-when">{new Date(v.changed_at).toLocaleString()}</span>
                </button>
                {i > 0 && <button className="linkbtn" onClick={() => doRestore(v)}>Restore</button>}
              </div>
              {expanded === v.version_no && diff && (
                <div className="uc-hist-diff">
                  {Object.keys(diff.fields).length === 0
                    ? <span className="settings-hint">No field changes recorded for this version.</span>
                    : Object.entries(diff.fields).map(([f, ch]) => (
                        <div key={f} className="uc-hist-field">
                          <span className="uc-hist-fname">{f.replace(/_/g, " ")}</span>
                          <span className="uc-diff-before">{fmt(ch.before)}</span>
                          <span className="uc-diff-arr">→</span>
                          <span className="uc-diff-after">{fmt(ch.after)}</span>
                        </div>
                      ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
