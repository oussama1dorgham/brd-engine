import { useCallback, useEffect, useState } from "react";
import {
  addRequirement, approveDraft, deleteRequirement, discardDraft, getRequirements, getVersions,
  restoreVersion, revertRequirement, snapshotVersion, updateRequirement,
  type BrdVersion, type Requirement,
} from "../lib/api";

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
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [newText, setNewText] = useState("");
  const [newReqId, setNewReqId] = useState("");
  const [label, setLabel] = useState("");

  const reload = useCallback(async () => {
    if (!project) return;
    const [rs, v] = await Promise.all([
      getRequirements(project).catch(() => []),
      getVersions(project).catch(() => ({ review_status: "approved", versions: [] as BrdVersion[] })),
    ]);
    setReqs(rs); setStatus(v.review_status); setVersions(v.versions);
  }, [project]);

  useEffect(() => { if (open) reload(); }, [open, reload]);

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

  const saveEdit = (chunk_id: number) => run(() => updateRequirement(chunk_id, draft.trim()), "Requirement updated").then(() => setEditing(null));

  return (
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="modal reqmodal" role="dialog" aria-modal="true">
        <div className="modal-h">
          <h2>Requirements — {projLabel}</h2>
          <button className="modal-x" aria-label="Close" onClick={() => { if (!busy) onClose(); }}>✕</button>
        </div>

        {status === "draft" && (
          <div className="draftbar">
            <span>⚠ This BRD has unreviewed changes.</span>
            <button className="primary" disabled={busy} onClick={() => run(() => approveDraft(project), "Changes approved ✓")}>Approve</button>
            <button className="backbtn" disabled={busy} onClick={() => run(() => discardDraft(project), "Draft discarded — restored last approved")}>Discard</button>
          </div>
        )}

        <div className="reqadd">
          <input className="reqid-in" value={newReqId} onChange={(e) => setNewReqId(e.target.value)} placeholder="ID (e.g. FR-14, optional)" />
          <textarea value={newText} onChange={(e) => setNewText(e.target.value)} placeholder="Add a new requirement…" rows={2} />
          <button className="primary" disabled={busy || !newText.trim()}
                  onClick={() => run(() => addRequirement(project, newText.trim(), newReqId.trim() || undefined), "Requirement added")
                    .then(() => { setNewText(""); setNewReqId(""); })}>Add</button>
        </div>

        <div className="reqlist">
          {reqs.map((r) => (
            <div key={r.chunk_id} className="reqrow">
              <div className="reqhead">
                <span className="reqid">{r.req_id || r.section || `#${r.ordinal}`}</span>
                <div className="reqacts">
                  {editing === r.chunk_id ? (
                    <>
                      <button className="linkbtn" disabled={busy} onClick={() => saveEdit(r.chunk_id)}>Save</button>
                      <button className="linkbtn" onClick={() => setEditing(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <button className="linkbtn" onClick={() => { setEditing(r.chunk_id); setDraft(r.text); }}>Edit</button>
                      <button className="linkbtn" disabled={busy} onClick={() => run(() => revertRequirement(r.chunk_id), "Reverted to previous")}>Revert</button>
                      <button className="linkbtn danger" disabled={busy} onClick={() => run(() => deleteRequirement(r.chunk_id), "Requirement removed")}>Delete</button>
                    </>
                  )}
                </div>
              </div>
              {editing === r.chunk_id
                ? <textarea className="reqedit" value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} autoFocus />
                : <div className="reqtext" dir="auto">{r.text}</div>}
            </div>
          ))}
          {reqs.length === 0 && <p className="settings-hint">No requirements found for this BRD.</p>}
        </div>

        <div className="reqversions">
          <h3>Versions</h3>
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
        </div>
      </div>
    </div>
  );
}
