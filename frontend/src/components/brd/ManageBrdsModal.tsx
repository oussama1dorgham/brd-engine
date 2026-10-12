import { useEffect, useRef, useState } from "react";
import type { Project } from "../../types";
import { MAX_UPLOAD_BYTES, sourceDownloadUrl, type UploadResult } from "../../lib/api";
import { etaText } from "../../lib/format";

const PENCIL_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>';
const TRASH_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
const UP_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/><path d="M5 21h14"/></svg>';
const REQ_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/></svg>';
const X_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg>';
const REPROCESS_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';
const DOWN_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/></svg>';

const fmtSize = (n: number) => (n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + " MB" : Math.round(n / 1024) + " KB");

// Why a job is retrying (and when it'll try again), surfaced as a tooltip on the status pill —
// covers both a brand-new upload and a revision, since both share the same IngestJobState shape.
const jobRetryTitle = (job?: Project["job"]): string | undefined => {
  if (!job?.error) return undefined;
  const when = job.retry_at ? ` — retrying at ${new Date(job.retry_at).toLocaleTimeString()}` : "";
  return `Retrying: ${job.error}${when}`;
};

export default function ManageBrdsModal({
  open,
  onClose,
  projects,
  toast,
  onUpload,
  onRename,
  onDelete,
  onEditRequirements,
  onReview,
  onRetry,
  onCancelJob,
  onReprocess,
  confirm,
  confirmOpen,
}: {
  open: boolean;
  onClose: () => void;
  projects: Project[];
  toast: (m: string) => void;
  // resolves the upload result (null = failed); a {conflict} result means "ask the user"
  onUpload: (file: File, name: string, opts?: { project?: string; asNew?: boolean }) => Promise<UploadResult | null>;
  onRename: (project: string, title: string) => Promise<boolean>;
  onDelete: (project: string) => void;
  onEditRequirements: (project: string) => void;
  onReview: (project: string) => void;             // open the new-version comparison
  onRetry: (jobId: number) => void;                // retry a failed ingestion
  onCancelJob: (jobId: number) => void;            // cancel a queued/running ingestion job
  onReprocess: (project: string) => void;          // re-process from the stored original file
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;
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
  const [conflict, setConflict] = useState<{ project: string; title: string } | null>(null);
  const versionRef = useRef<HTMLInputElement>(null);
  const [versionFor, setVersionFor] = useState<string | null>(null);   // project a new version is for
  const [versionUploading, setVersionUploading] = useState<string | null>(null);   // project whose version is in flight

  useEffect(() => {
    if (open) { setTimeout(() => nameRef.current?.focus(), 40); }
    else { setChosen(null); setName(""); setEditing(null); setConflict(null); if (fileRef.current) fileRef.current.value = ""; }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !uploading && !confirmOpen) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, uploading, confirmOpen, onClose]);

  if (!open) return null;

  const uploadLabel = uploading ? "Uploading…" : chosen ? "Confirm & ingest" : "Upload & ingest";

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    if (ext !== "pdf" && ext !== "docx") { toast("Only .pdf or .docx files are supported"); if (fileRef.current) fileRef.current.value = ""; return; }
    if (f.size > MAX_UPLOAD_BYTES) {
      toast(`"${f.name}" is ${fmtSize(f.size)} — the limit is ${fmtSize(MAX_UPLOAD_BYTES)}`);
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setConflict(null);   // a freshly picked file is unrelated to whatever earlier upload conflicted
    setChosen(f);
    setName((n) => (n.trim() ? n : f.name.replace(/\.(pdf|docx)$/i, "")));
  };

  const reset = () => { setChosen(null); setName(""); setConflict(null); if (fileRef.current) fileRef.current.value = ""; };
  const doUpload = async (opts?: { project?: string; asNew?: boolean }) => {
    if (uploading) return;
    if (!chosen) { fileRef.current?.click(); return; }
    setUploading(true);
    const r = await onUpload(chosen, name, opts);
    setUploading(false);
    if (r?.conflict) { setConflict(r.conflict); return; }       // same name: ask what the user intends
    if (r) reset();
  };
  const pickVersion = (project: string) => { setVersionFor(project); versionRef.current?.click(); };
  const onVersionFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (versionRef.current) versionRef.current.value = "";
    const proj = versionFor;
    setVersionFor(null);
    if (!f || !proj) return;
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    if (ext !== "pdf" && ext !== "docx") { toast("Only .pdf or .docx files are supported"); return; }
    if (f.size > MAX_UPLOAD_BYTES) {
      toast(`"${f.name}" is ${fmtSize(f.size)} — the limit is ${fmtSize(MAX_UPLOAD_BYTES)}`);
      return;
    }
    setVersionUploading(proj);
    try {
      await onUpload(f, "", { project: proj });
    } finally {
      setVersionUploading(null);
    }
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

  const doReprocess = async (p: Project) => {
    if (!(await confirm(
      `Re-process “${p.title || p.project}” from its originally uploaded file? This creates a new version to review — ` +
      `nothing in the current BRD changes until you apply it.`,
      "Re-process",
    ))) return;
    onReprocess(p.project);
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
              <input ref={nameRef} type="text" placeholder="e.g. Payments System v2" maxLength={120} autoComplete="off" dir="auto" value={name}
                onChange={(e) => { setName(e.target.value); if (conflict) setConflict(null); }} />
            </label>
            <input ref={fileRef} type="file" accept=".pdf,.docx" hidden onChange={onFile} />
            <input ref={versionRef} type="file" accept=".pdf,.docx" hidden onChange={onVersionFile} />
            {conflict ? (
              <div className="brd-conflict">
                <b>“{conflict.title}” already exists.</b> What is this file?
                <div className="brd-conflict-acts">
                  <button className="primary" disabled={uploading} onClick={() => doUpload({ project: conflict.project })}>
                    A new version of it — let me review the changes
                  </button>
                  <button className="backbtn" disabled={uploading} onClick={() => doUpload({ asNew: true })}>A separate BRD — keep both</button>
                  <button className="linkbtn" disabled={uploading} onClick={() => setConflict(null)}>Cancel</button>
                </div>
              </div>
            ) : (
              <button className="primary" disabled={uploading} onClick={() => doUpload()}>{uploadLabel}</button>
            )}
            {chosen && (
              <p className="fname">
                Selected: {chosen.name}{"  "}
                <button type="button" className="linkbtn" onClick={() => { if (!uploading) fileRef.current?.click(); }}>change</button>
              </p>
            )}
            <p className="mnote">
              Click <b>Upload &amp; ingest</b> to pick a <code>.pdf</code> or <code>.docx</code> (up to {fmtSize(MAX_UPLOAD_BYTES)}), then
              confirm. Processing runs in the background — you can keep working. To update a BRD, use <b>⬆</b> on its row: the new version
              is compared with the current one and nothing changes until you review and apply it.
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
                        <div className="bt" dir="auto">{p.title || p.project}</div>
                        <div className="bm">{p.project} · {p.docs} doc{p.docs === 1 ? "" : "s"}</div>
                      </div>
                      <div className="bstate">
                      {p.status === "processing" && (
                        <span className="bstatus processing" title={jobRetryTitle(p.job)}>
                          <span className="dot" />
                          {`Processing ${typeof p.progress === "number" ? p.progress + "%" : "…"}${etaText(p.eta_seconds) ? " · " + etaText(p.eta_seconds) : ""}`}
                        </span>
                      )}
                      {p.status === "failed" && (
                        <span className="bstatus failed" title={p.error || "Processing failed"}>
                          Failed{p.error ? ` — ${p.error.length > 60 ? p.error.slice(0, 60) + "…" : p.error}` : ""}
                        </span>
                      )}
                      {p.status === "failed" && p.job?.status === "failed" && (
                        <button className="br-save" onClick={() => onRetry(p.job!.id)}>Retry</button>
                      )}
                      {p.status === "ready" && p.job?.mode === "revision" && (p.job.status === "queued" || p.job.status === "running") && (
                        <span className="bstatus processing" title={jobRetryTitle(p.job)}>
                          <span className="dot" />
                          New version: {p.job.stage || "queued"}{p.job.total ? ` ${Math.round((100 * p.job.done) / p.job.total)}%` : ""}
                        </span>
                      )}
                      {p.status === "ready" && p.job?.mode === "revision" && p.job.status === "failed" && (
                        <span className="bstatus failed" title={p.job.error || ""}>New version failed{" "}
                          <button className="linkbtn" onClick={() => onRetry(p.job!.id)}>retry</button></span>
                      )}
                      {!!p.job?.warnings?.length && (
                        <span className="bstatus warn" title={p.job.warnings.join("; ")}>
                          ⚠ {p.job.warnings.length} warning{p.job.warnings.length > 1 ? "s" : ""}
                        </span>
                      )}
                      {p.review && (
                        <button className="br-review" onClick={() => onReview(p.project)}
                                title="A new version was compared with the current BRD — review it before anything changes">
                          Review new version ({p.review.summary.modified + p.review.summary.added + p.review.summary.removed})
                        </button>
                      )}
                      {p.status === "ready" && p.outdated && !p.review && !(p.job && (p.job.status === "queued" || p.job.status === "running")) && (
                        p.has_source ? (
                          <button className="br-review" onClick={() => doReprocess(p)}
                                  title="This BRD was split into requirements by an older version of ingestion. Re-process it from its original file so questions find and cite each requirement correctly — you review the result before anything changes.">
                            Improve: re-process
                          </button>
                        ) : (
                          <button className="br-review" onClick={() => pickVersion(p.project)}
                                  disabled={versionUploading === p.project}
                                  title="This BRD was split into requirements by an older version of ingestion and its original file wasn't kept. Upload the original Word/PDF again as a new version so questions find and cite each requirement correctly — you review the result before anything changes.">
                            Improve: upload original again
                          </button>
                        )
                      )}
                      {versionUploading === p.project && (
                        <span className="bstatus processing"><span className="dot" />Uploading…</span>
                      )}
                      </div>
                      <div className="bacts">
                      {p.job && (p.job.status === "queued" || p.job.status === "running") && (
                        <button className="br" title="Cancel this job" aria-label={"Cancel ingestion for " + (p.title || p.project)}
                          onClick={() => onCancelJob(p.job!.id)}
                          dangerouslySetInnerHTML={{ __html: X_SVG }} />
                      )}
                      <button className="br" title="Upload a new version of this BRD (compared before anything changes)"
                        aria-label={"Upload a new version of " + (p.title || p.project)}
                        disabled={p.status === "processing" || !!p.review || p.job?.status === "queued" || p.job?.status === "running" || versionUploading === p.project}
                        onClick={() => pickVersion(p.project)}
                        dangerouslySetInnerHTML={{ __html: UP_SVG }} />
                      {p.has_source && (
                        <button className="br" title="Re-process this BRD from its original file (creates a new version to review)"
                          aria-label={"Re-process " + (p.title || p.project)}
                          disabled={p.status === "processing" || !!p.review || p.job?.status === "queued" || p.job?.status === "running"}
                          onClick={() => doReprocess(p)}
                          dangerouslySetInnerHTML={{ __html: REPROCESS_SVG }} />
                      )}
                      {p.has_source && p.source_id != null && (
                        <a className="br" title="Download the original uploaded file" aria-label={"Download original file of " + (p.title || p.project)}
                          href={sourceDownloadUrl(p.source_id)}
                          dangerouslySetInnerHTML={{ __html: DOWN_SVG }} />
                      )}
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
