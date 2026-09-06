import { useEffect, useRef, useState } from "react";
import type { Project } from "../types";
import { etaText } from "../lib/format";

const PENCIL_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>';
const TRASH_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
const REQ_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/></svg>';

export default function ManageBrdsModal({
  open,
  onClose,
  projects,
  toast,
  onUpload,
  onRename,
  onDelete,
  onEditRequirements,
  confirmOpen,
}: {
  open: boolean;
  onClose: () => void;
  projects: Project[];
  toast: (m: string) => void;
  onUpload: (file: File, name: string) => Promise<boolean>;
  onRename: (project: string, title: string) => Promise<boolean>;
  onDelete: (project: string) => void;
  onEditRequirements: (project: string) => void;
  confirmOpen: boolean;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editVal, setEditVal] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);

  useEffect(() => {
    if (open) { setTimeout(() => nameRef.current?.focus(), 40); }
    else { setChosen(null); setName(""); setEditing(null); if (fileRef.current) fileRef.current.value = ""; }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !uploading && !confirmOpen) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, uploading, confirmOpen, onClose]);

  if (!open) return null;

  const uploadLabel = uploading ? "Ingesting… (embedding)" : chosen ? "Confirm & ingest" : "Upload & ingest";

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    if (ext !== "pdf" && ext !== "docx") { toast("Only .pdf or .docx files are supported"); if (fileRef.current) fileRef.current.value = ""; return; }
    setChosen(f);
    setName((n) => (n.trim() ? n : f.name.replace(/\.(pdf|docx)$/i, "")));
  };

  const doUpload = async () => {
    if (uploading) return;
    if (!chosen) { fileRef.current?.click(); return; }
    setUploading(true);
    const ok = await onUpload(chosen, name);
    setUploading(false);
    if (ok) { setChosen(null); setName(""); if (fileRef.current) fileRef.current.value = ""; }
  };

  const saveEdit = async (p: Project) => {
    const title = editVal.trim();
    if (!title) { toast("Name cannot be empty"); return; }
    if (title === (p.title || p.project)) { setEditing(null); return; }
    setSavingEdit(true);
    const ok = await onRename(p.project, title);
    setSavingEdit(false);
    if (ok) setEditing(null);
  };

  return (
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget && !uploading) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="brdmodal-title">
        <div className="modal-head">
          <h2 id="brdmodal-title">Manage BRDs</h2>
          <button className="modal-x" aria-label="Close" onClick={() => { if (!uploading) onClose(); }}>✕</button>
        </div>
        <div className="modal-body">
          <section className="msec">
            <h3>Add a BRD</h3>
            <label className="fld">
              <span>Name <em>(what you'll see in the list — optional)</em></span>
              <input ref={nameRef} type="text" placeholder="e.g. Payments System v2" maxLength={120} autoComplete="off" dir="auto" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <input ref={fileRef} type="file" accept=".pdf,.docx" hidden onChange={onFile} />
            <button className="primary" disabled={uploading} onClick={doUpload}>{uploadLabel}</button>
            {chosen && (
              <p className="fname">
                Selected: {chosen.name}{"  "}
                <button type="button" className="linkbtn" onClick={() => { if (!uploading) fileRef.current?.click(); }}>change</button>
              </p>
            )}
            <p className="mnote">
              Click <b>Upload &amp; ingest</b> to pick a <code>.pdf</code> or <code>.docx</code>, then confirm — parsing,
              chunking &amp; embedding run on the server (this can take a moment).
            </p>
          </section>
          <section className="msec">
            <h3>Your BRDs</h3>
            <div className="brdlist">
              {projects.length === 0 ? (
                <div className="brdempty">No BRDs yet — add one above.</div>
              ) : (
                projects.map((p) =>
                  editing === p.project ? (
                    <div key={p.project} className="brdrow">
                      <div className="brdedit">
                        <input
                          autoFocus
                          dir="auto"
                          maxLength={120}
                          value={editVal}
                          onChange={(e) => setEditVal(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") { e.preventDefault(); saveEdit(p); }
                            else if (e.key === "Escape") setEditing(null);
                          }}
                        />
                        <button className="br-save" disabled={savingEdit} onClick={() => saveEdit(p)}>Save</button>
                        <button className="br-cancel" disabled={savingEdit} onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                    </div>
                  ) : (
                    <div key={p.project} className="brdrow">
                      <div className="bi">
                        <div className="bt">{p.title || p.project}</div>
                        <div className="bm">{p.project} · {p.docs} doc{p.docs === 1 ? "" : "s"}</div>
                      </div>
                      {p.status === "processing" && (
                        <span className="bstatus processing">
                          <span className="dot" />
                          {`Processing ${typeof p.progress === "number" ? p.progress + "%" : "…"}${etaText(p.eta_seconds) ? " · " + etaText(p.eta_seconds) : ""}`}
                        </span>
                      )}
                      {p.status === "failed" && <span className="bstatus failed">Failed</span>}
                      <button className="br" title="Edit requirements" aria-label={"Edit requirements of " + (p.title || p.project)}
                        disabled={p.status === "processing"}
                        onClick={() => onEditRequirements(p.project)}
                        dangerouslySetInnerHTML={{ __html: REQ_SVG }} />
                      <button className="br" title="Rename this BRD" aria-label={"Rename " + (p.title || p.project)}
                        disabled={p.status === "processing"}
                        onClick={() => { setEditing(p.project); setEditVal(p.title || p.project); }}
                        dangerouslySetInnerHTML={{ __html: PENCIL_SVG }} />
                      <button className="bd" title="Delete this BRD" aria-label={"Delete " + (p.title || p.project)}
                        onClick={() => onDelete(p.project)} dangerouslySetInnerHTML={{ __html: TRASH_SVG }} />
                    </div>
                  ),
                )
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
