import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addRequirement, approveDraft, deleteRequirement, discardDraft, getRequirements, getVersions,
  proposeRowSplit, restoreVersion, revertRequirement, snapshotVersion, splitRequirement, updateRequirement,
  type BrdVersion, type Requirement,
} from "../lib/api";

// A single-line blob carrying multiple "|" is a flattened table (rows fused into
// one chunk) — offer to break it into individually-editable rows.
const looksFlattened = (t: string) => !t.includes("\n") && (t.match(/\|/g) || []).length >= 2;

// escape a user string for safe use inside a RegExp
const rescape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// split text into [plain, <mark>, plain, …] segments for every occurrence of `q`
function highlight(text: string, q: string) {
  if (!q) return text;
  const re = new RegExp(rescape(q), "gi");
  const out: (string | JSX.Element)[] = [];
  let last = 0, m: RegExpExecArray | null, i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(<mark key={i++} className="reqhl">{m[0]}</mark>);
    last = m.index + m[0].length;
    if (m.index === re.lastIndex) re.lastIndex++;   // guard against zero-width
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function RequirementsModal({ open, project, projLabel, onClose, toast, onChanged }: {
  open: boolean;
  project: string | null;
  projLabel: string;
  onClose: () => void;
  toast: (m: string) => void;
  onChanged?: () => void;   // notify the app that the BRD changed (answers may differ)
}) {
  const [reqs, setReqs] = useState<Requirement[]>([]);
  const [status, setStatus] = useState<string>("approved");
  const [versions, setVersions] = useState<BrdVersion[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [confirmDel, setConfirmDel] = useState<number | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [newText, setNewText] = useState("");
  const [newReqId, setNewReqId] = useState("");
  const [label, setLabel] = useState("");
  const [query, setQuery] = useState("");
  const [showVersions, setShowVersions] = useState(false);
  const [splitFor, setSplitFor] = useState<number | null>(null);   // chunk being split into rows
  const [splitRows, setSplitRows] = useState<string[]>([]);
  const [splitLoading, setSplitLoading] = useState(false);

  const reload = useCallback(async () => {
    if (!project) return;
    setLoading(true);
    const [rs, v] = await Promise.all([
      getRequirements(project).catch(() => []),
      getVersions(project).catch(() => ({ review_status: "approved", versions: [] as BrdVersion[] })),
    ]);
    setReqs(rs); setStatus(v.review_status); setVersions(v.versions);
    setLoading(false);
  }, [project]);

  useEffect(() => {
    if (open) {
      setQuery(""); setEditing(null); setShowVersions(false); setSplitFor(null);
      setConfirmDel(null); setShowAdd(false); setNewText(""); setNewReqId("");
      reload();
    }
  }, [open, reload]);

  // Esc cancels the active sub-action first (edit / split / add), else closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || busy) return;
      if (editing !== null) setEditing(null);
      else if (splitFor !== null) setSplitFor(null);
      else if (confirmDel !== null) setConfirmDel(null);
      else if (showAdd) setShowAdd(false);
      else onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, busy, editing, splitFor, confirmDel, showAdd, onClose]);

  const startSplit = async (chunk_id: number) => {
    setEditing(null); setConfirmDel(null);
    setSplitFor(chunk_id); setSplitRows([]); setSplitLoading(true);
    try {
      const r = await proposeRowSplit(chunk_id);
      if (r.error) { toast(r.error); setSplitFor(null); }
      else if (!r.rows || r.rows.length < 2) { toast("Couldn't detect separate rows — edit it manually."); setSplitFor(null); }
      else setSplitRows(r.rows);
    } catch (e) { toast((e as Error).message); setSplitFor(null); }
    setSplitLoading(false);
  };

  const applySplit = (chunk_id: number) => {
    const rows = splitRows.map((r) => r.trim()).filter(Boolean);
    if (rows.length < 2) { toast("Need at least two rows"); return; }
    run(() => splitRequirement(chunk_id, rows), `Split into ${rows.length} requirements`).then(() => setSplitFor(null));
  };

  // live keyword filter over id / section / text
  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!q) return reqs;
    return reqs.filter((r) =>
      r.text.toLowerCase().includes(q) ||
      (r.req_id || "").toLowerCase().includes(q) ||
      (r.section || "").toLowerCase().includes(q));
  }, [reqs, q]);

  if (!open || !project) return null;

  const run = async (fn: () => Promise<{ error?: string }>, ok: string) => {
    setBusy(true);
    try {
      const r = await fn();
      if (r && r.error) { toast(r.error); }
      else { toast(ok); await reload(); onChanged?.(); }
    } catch (e) { toast((e as Error).message); }
    setBusy(false);
  };

  const beginEdit = (r: Requirement) => { setSplitFor(null); setConfirmDel(null); setEditing(r.chunk_id); setDraft(r.text); };
  const saveEdit = (chunk_id: number) => run(() => updateRequirement(chunk_id, draft.trim()), "Requirement updated").then(() => setEditing(null));
  const doAdd = () => run(() => addRequirement(project, newText.trim(), newReqId.trim() || undefined), "Requirement added")
    .then(() => { setNewText(""); setNewReqId(""); setShowAdd(false); });

  return (
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="modal reqmodal" role="dialog" aria-modal="true" aria-labelledby="reqmodal-title">
        <div className="modal-h reqmodal-h">
          <div className="reqtitle">
            <h2 id="reqmodal-title">Requirements</h2>
            <span className="reqproj" dir="auto" title={projLabel}>{projLabel}</span>
          </div>
          <div className="reqmeta">
            {status === "draft" && <span className="reqpill draft">● Draft</span>}
            {!loading && <span className="reqpill">{reqs.length} {reqs.length === 1 ? "req" : "reqs"}</span>}
          </div>
          <button className="modal-x" aria-label="Close" onClick={() => { if (!busy) onClose(); }}>✕</button>
        </div>

        {status === "draft" && (
          <div className="draftbar">
            <span>⚠ This BRD has unreviewed changes — they’re live, but not yet approved.</span>
            <button className="primary" disabled={busy} onClick={() => run(() => approveDraft(project), "Changes approved ✓")}>Approve</button>
            <button className="backbtn" disabled={busy} onClick={() => run(() => discardDraft(project), "Draft discarded — restored last approved")}>Discard</button>
          </div>
        )}

        <div className="reqtoolbar">
          <div className="reqsearch">
            <span className="reqsearch-ico" aria-hidden="true">🔎</span>
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by keyword, ID or section…"
              dir="auto"
              aria-label="Search requirements"
            />
            {query && <button className="reqsearch-x" aria-label="Clear search" onClick={() => setQuery("")}>✕</button>}
            {q && <span className="reqcount">{filtered.length} / {reqs.length}</span>}
          </div>
          <button className="reqadd-toggle" aria-expanded={showAdd}
                  onClick={() => setShowAdd((s) => !s)}>{showAdd ? "✕ Cancel" : "+ Add requirement"}</button>
        </div>

        {showAdd && (
          <div className="reqadd">
            <input className="reqid-in" value={newReqId} autoFocus onChange={(e) => setNewReqId(e.target.value)} placeholder="ID (e.g. FR-14, optional)" />
            <textarea value={newText} onChange={(e) => setNewText(e.target.value)}
                      onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && newText.trim()) doAdd(); }}
                      placeholder="New requirement text…  (⌘/Ctrl+Enter to add)" rows={2} />
            <button className="primary" disabled={busy || !newText.trim()} onClick={doAdd}>Add</button>
          </div>
        )}

        <div className="reqlist">
          {filtered.map((r) => (
            <div key={r.chunk_id} className={"reqrow" + (editing === r.chunk_id || splitFor === r.chunk_id ? " active" : "")}>
              <div className="reqhead">
                <span className="reqid" dir="auto">{highlight(r.req_id || r.section || `#${r.ordinal}`, q)}</span>
                <div className="reqacts">
                  {editing === r.chunk_id ? (
                    <>
                      <button className="linkbtn strong" disabled={busy} onClick={() => saveEdit(r.chunk_id)}>Save</button>
                      <button className="linkbtn" onClick={() => setEditing(null)}>Cancel</button>
                    </>
                  ) : splitFor === r.chunk_id ? (
                    <span className="reqacts-hint">Splitting…</span>
                  ) : confirmDel === r.chunk_id ? (
                    <>
                      <span className="reqacts-hint">Delete?</span>
                      <button className="linkbtn danger" disabled={busy}
                              onClick={() => run(() => deleteRequirement(r.chunk_id), "Requirement removed").then(() => setConfirmDel(null))}>Yes, delete</button>
                      <button className="linkbtn" onClick={() => setConfirmDel(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <button className="linkbtn" onClick={() => beginEdit(r)}>Edit</button>
                      <button className="linkbtn" disabled={busy} title="Undo the last change to this requirement"
                              onClick={() => run(() => revertRequirement(r.chunk_id), "Reverted to previous")}>Revert</button>
                      <button className="linkbtn danger" onClick={() => { setConfirmDel(r.chunk_id); setEditing(null); }}>Delete</button>
                    </>
                  )}
                </div>
              </div>

              {editing === r.chunk_id ? (
                <textarea className="reqedit" value={draft} autoFocus dir="auto" rows={4}
                          onChange={(e) => setDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") saveEdit(r.chunk_id);
                            else if (e.key === "Escape") { e.stopPropagation(); setEditing(null); }
                          }} />
              ) : splitFor === r.chunk_id ? (
                <div className="splitpanel">
                  {splitLoading ? (
                    <p className="settings-hint">Detecting rows… <span className="dimmed">content is preserved exactly — the model only chooses where each row begins</span></p>
                  ) : (
                    <>
                      <p className="settings-hint">Review the detected rows — each becomes its own requirement. Every row is a literal slice of the original, so nothing is added or lost.</p>
                      {splitRows.map((rowText, i) => (
                        <div key={i} className="splitrow">
                          <span className="splitnum">{i + 1}</span>
                          <textarea value={rowText} dir="auto" rows={2}
                                    onChange={(e) => setSplitRows((rs) => rs.map((v, j) => (j === i ? e.target.value : v)))} />
                          <button className="splitdel" title="Remove this row" disabled={busy}
                                  onClick={() => setSplitRows((rs) => rs.filter((_, j) => j !== i))}>✕</button>
                        </div>
                      ))}
                      <div className="splitacts">
                        <button className="linkbtn" disabled={busy} onClick={() => setSplitRows((rs) => [...rs, ""])}>+ Add row</button>
                        <span className="splitspacer" />
                        <button className="backbtn" disabled={busy} onClick={() => setSplitFor(null)}>Cancel</button>
                        <button className="primary" disabled={busy || splitRows.filter((x) => x.trim()).length < 2}
                                onClick={() => applySplit(r.chunk_id)}>Split into {splitRows.filter((x) => x.trim()).length} rows</button>
                      </div>
                    </>
                  )}
                </div>
              ) : (
                <>
                  <div className="reqtext" dir="auto">{highlight(r.text, q)}</div>
                  {looksFlattened(r.text) && (
                    <div className="reqflag">
                      <span>⚠ This looks like a table squeezed into one requirement.</span>
                      <button className="linkbtn strong" disabled={busy} onClick={() => startSplit(r.chunk_id)}>Split into rows</button>
                    </div>
                  )}
                </>
              )}
            </div>
          ))}
          {!loading && reqs.length === 0 && (
            <div className="reqempty">
              <p className="settings-hint">No requirements in this BRD yet.</p>
              <button className="linkbtn strong" onClick={() => setShowAdd(true)}>+ Add the first one</button>
            </div>
          )}
          {!loading && reqs.length > 0 && filtered.length === 0 && (
            <p className="settings-hint">No requirements match “{query.trim()}”.</p>
          )}
          {loading && <p className="settings-hint">Loading requirements…</p>}
        </div>

        <div className="reqversions">
          <button className="reqver-toggle" onClick={() => setShowVersions((s) => !s)} aria-expanded={showVersions}>
            <span className={"reqver-caret" + (showVersions ? " open" : "")}>▸</span>
            Version history {versions.length > 0 && <span className="reqver-badge">{versions.length}</span>}
          </button>
          {showVersions && (
            <>
              <p className="settings-hint reqver-note">Save a snapshot before big changes — restore any snapshot to roll the whole BRD back.</p>
              <div className="reqadd">
                <input className="reqid-in" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Version label (optional)" />
                <button className="backbtn" disabled={busy}
                        onClick={() => run(() => snapshotVersion(project, label.trim()), "Version saved").then(() => setLabel(""))}>Save current version</button>
              </div>
              {versions.map((v) => (
                <div key={v.id} className="verrow">
                  <span className="vertag">{v.kind}</span>
                  <span className="vermain">{v.label || "(no label)"} · {v.requirements} reqs · {new Date(v.created_at).toLocaleString()}</span>
                  <button className="linkbtn" disabled={busy} onClick={() => run(() => restoreVersion(project, v.id), "Version restored")}>Restore</button>
                </div>
              ))}
              {versions.length === 0 && <p className="settings-hint">No saved versions yet.</p>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
