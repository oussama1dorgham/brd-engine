import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  createUcFolder, deleteUcFolder, deleteUseCase, generateUseCases, getRequirements,
  getUseCaseStatus, getUseCases, moveUseCase, renameUcFolder, updateUseCase,
  type Requirement, type UseCase, type UseCaseFolder, type UseCaseStatus,
} from "../lib/api";
import { useResizable } from "../lib/useResizable";
import UseCaseHistory from "./UseCaseHistory";
import { getDeletedUseCases, restoreUseCase, type UcDeleted } from "../lib/api";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="uc-field"><div className="uc-flabel">{label}</div><div className="uc-fval">{children}</div></div>;
}

const countAll = (fs: UseCaseFolder[]): number =>
  fs.reduce((n, f) => n + f.use_cases.length + countAll(f.children), 0);

const findUc = (fs: UseCaseFolder[], id: number): UseCase | null => {
  for (const f of fs) {
    const hit = f.use_cases.find((u) => u.id === id) || findUc(f.children, id);
    if (hit) return hit;
  }
  return null;
};

const flatten = (fs: UseCaseFolder[], depth = 0): { id: number; label: string }[] =>
  fs.flatMap((f) => [{ id: f.id, label: `${"  ".repeat(depth)}${f.name}` }, ...flatten(f.children, depth + 1)]);

// A dedicated page: use cases derived from one BRD, organized as a folder tree.
// On first open (Option B) it auto-generates in the background if none exist yet,
// filling the tree in as scopes complete; once generated they're stored (instant next time).
export default function UseCasesPage({ project, projLabel, toast, model }: {
  project: string | null;
  projLabel: string;
  toast: (m: string) => void;
  model: string | null;   // generation model selected in the chat; used for generation too
}) {
  const [folders, setFolders] = useState<UseCaseFolder[]>([]);
  const [loading, setLoading] = useState(false);
  const [gen, setGen] = useState(false);
  const [status, setStatus] = useState<UseCaseStatus | null>(null);
  const [selected, setSelected] = useState<UseCase | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [reqMap, setReqMap] = useState<Record<number, Requirement>>({});
  const [warn, setWarn] = useState<string | null>(null);   // persistent notice (partial failure)
  const [trashOpen, setTrashOpen] = useState(false);       // Trash view (deleted/superseded cards)
  const [deleted, setDeleted] = useState<UcDeleted[]>([]);
  const pollRef = useRef<number | null>(null);
  const autoRef = useRef<string | null>(null);   // project we've already auto-generated for
  const sigRef = useRef<string>("");              // last-rendered tree signature (skip no-op re-renders)

  const load = useCallback(async (): Promise<number> => {
    if (!project) return 0;
    const d = await getUseCases(project).catch(() => ({ folders: [] as UseCaseFolder[] }));
    const fs = d.folders || [];
    // The generation poll runs every 2s but scopes complete far less often, so most
    // polls return the identical tree. Re-setting state each time re-renders the whole
    // list and jitters the user's scroll ("trembling"). Only commit when it changed.
    const sig = JSON.stringify(fs);
    if (sig !== sigRef.current) {
      sigRef.current = sig;
      setFolders(fs);
      setExpanded((prev) => (prev.size ? prev : new Set(fs.map((f) => f.id))));  // default-open top level once
      setSelected((sel) => (sel ? findUc(fs, sel.id) : null));                    // keep detail panel in sync
    }
    return countAll(fs);
  }, [project]);

  const stopPoll = () => { if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; } };

  const startPolling = useCallback(() => {
    if (pollRef.current || !project) return;
    setGen(true);
    pollRef.current = window.setInterval(async () => {
      const st = await getUseCaseStatus(project).catch(() => null);
      setStatus(st);
      await load();   // progressive: tree fills in as scopes complete
      if (st && !st.running) {
        stopPoll();
        setGen(false);
        const failed = st?.errors?.length ?? 0;
        const pending = st?.resume?.pending ?? 0;
        if (st?.error) { setWarn(st.error); toast(st.error); }
        else if (failed || pending) {
          const left = pending || failed;
          const msg = `Generated ${st.made ?? 0} use case(s), but ${left} batch(es) didn’t finish — likely rate limits. Click Resume to continue from where it stopped (no duplicates, no re-doing what’s done).`;
          setWarn(msg); toast(`${left} batch(es) left — click Resume`);
        } else { setWarn(null); toast(`Use cases ready — ${st.made ?? 0} generated ✓`); }
      }
    }, 2000);
  }, [project, load]);  // eslint-disable-line react-hooks/exhaustive-deps

  // "resume" (default): skip batches a prior run already wrote — continues a run stopped
  // by a quota limit / outage without re-generating or duplicating. "replace": wipe + redo.
  const runGenerate = useCallback(async (mode: "resume" | "sync" | "replace") => {
    if (!project) return;
    setWarn(null);
    setGen(true);   // flip immediately so the Generate button never flashes during the POST
    const r = await generateUseCases(project, mode === "replace", model, mode);
    if (r.error) { setGen(false); toast(r.error); return; }
    startPolling();
  }, [project, startPolling, toast, model]);

  useEffect(() => {
    if (!project) return;
    sigRef.current = "";   // force the first load for this project to commit
    setSelected(null); setStatus(null); setWarn(null); setLoading(true); stopPoll(); setGen(false);
    getRequirements(project)
      .then((rs) => setReqMap(Object.fromEntries(rs.map((r) => [r.chunk_id, r]))))
      .catch(() => setReqMap({}));
    (async () => {
      const st = await getUseCaseStatus(project).catch(() => null);
      setStatus(st);              // carries ledger-derived resume state when idle (Resume button)
      const n = await load();                                    // load() returns the count
      if (st?.running) { setLoading(false); startPolling(); return; }   // a job is already going
      if (n === 0 && autoRef.current !== project) {              // Option B: auto-generate once
        autoRef.current = project;
        setLoading(false);        // clears loading and (via runGenerate) sets gen in one tick — no button flash
        runGenerate("resume");    // empty ledger ⇒ generates everything from the start
        return;
      }
      setLoading(false);          // a partial tree is NOT auto-resumed — the user clicks Resume
    })();
    return stopPoll;
  }, [project]);  // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (id: number) =>
    setExpanded((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  // --- editing (QA) ---
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ title: "", description: "", roles: "", preconditions: "", steps: "", expected_behaviour: "" });

  const startEdit = () => {
    if (!selected) return;
    setDraft({
      title: selected.title, description: selected.description, roles: selected.roles.join(", "),
      preconditions: selected.preconditions, steps: selected.steps.join("\n"),
      expected_behaviour: selected.expected_behaviour,
    });
    setEditing(true);
  };
  const saveEdit = async () => {
    if (!selected) return;
    const title = draft.title.trim();
    if (!title) { toast("Title is required"); return; }
    const r = await updateUseCase(selected.id, {
      title, description: draft.description.trim(),
      roles: draft.roles.split(",").map((s) => s.trim()).filter(Boolean),
      preconditions: draft.preconditions.trim(),
      steps: draft.steps.split("\n").map((s) => s.trim()).filter(Boolean),
      expected_behaviour: draft.expected_behaviour.trim(),
    });
    if (r.error) { toast(r.error); return; }
    setEditing(false); await load(); toast("Saved ✓");
  };
  const doDelete = async () => {
    if (!selected || !window.confirm("Delete this use case?")) return;
    const r = await deleteUseCase(selected.id);
    if (r.error) { toast(r.error); return; }
    setSelected(null); setEditing(false); await load(); toast("Use case deleted");
  };
  const doMove = async (folderId: number) => {
    if (!selected) return;
    const r = await moveUseCase(selected.id, folderId);
    if (r.error) { toast(r.error); return; }
    await load(); toast("Moved ✓");
  };
  const newFolder = async (parentId: number | null) => {
    if (!project) return;
    const name = window.prompt(parentId ? "New sub-folder name:" : "New folder name:");
    if (!name || !name.trim()) return;
    const r = await createUcFolder(project, parentId, name.trim());
    if (r.error) { toast(r.error); return; }
    if (parentId) setExpanded((s) => new Set(s).add(parentId));
    await load();
  };
  const renameFolder = async (f: UseCaseFolder) => {
    const name = window.prompt("Rename folder:", f.name);
    if (!name || !name.trim()) return;
    const r = await renameUcFolder(f.id, name.trim());
    if (r.error) { toast(r.error); return; }
    await load();
  };
  const delFolder = async (f: UseCaseFolder) => {
    const cnt = f.use_cases.length + countAll(f.children);
    if (!window.confirm(`Delete folder "${f.name}"${cnt ? ` and its ${cnt} use case(s)` : ""}?`)) return;
    const r = await deleteUcFolder(f.id);
    if (r.error) { toast(r.error); return; }
    if (selected && !findUc(folders.filter((x) => x.id !== f.id), selected.id)) setSelected(null);
    await load();
  };

  const openTrash = useCallback(async () => {
    if (!project) return;
    const d = await getDeletedUseCases(project).catch(() => ({ deleted: [] as UcDeleted[] }));
    setDeleted(d.deleted || []);
    setTrashOpen(true);
  }, [project]);
  const restoreFromTrash = async (uid: number, version_no: number) => {
    const r = await restoreUseCase(uid, version_no);
    if (r.error) { toast(r.error); return; }
    toast("Restored ✓");
    await load();
    await openTrash();   // refresh the trash list
  };

  const count = countAll(folders);

  // Drag-resizable tree column width (persisted).
  const { size: treeWidth, onDragStart: onTreeResize } = useResizable({
    storageKey: "brd.ucTreeWidth", initial: 320, min: 220, max: 620, edge: "right",
  });

  const renderFolder = (f: UseCaseFolder, depth: number): ReactNode => (
    <div key={f.id} className="uc-folder">
      <div className="uc-foldrow" style={{ paddingLeft: 6 + depth * 14 }}>
        <button className="uc-foldhead" onClick={() => toggle(f.id)}>
          <span className="uc-caret">{expanded.has(f.id) ? "▾" : "▸"}</span>
          <span className="uc-foldname">📁 {f.name}</span>
          <span className="uc-count">{f.use_cases.length + countAll(f.children)}</span>
        </button>
        <span className="uc-foldacts">
          <button title="Rename folder" onClick={() => renameFolder(f)}>✎</button>
          <button title="New sub-folder" onClick={() => newFolder(f.id)}>＋</button>
          <button title="Delete folder" onClick={() => delFolder(f)}>✕</button>
        </span>
      </div>
      {expanded.has(f.id) && (
        <>
          {f.children.map((c) => renderFolder(c, depth + 1))}
          {f.use_cases.map((u) => (
            <button key={u.id} className={"uc-leaf" + (selected?.id === u.id ? " sel" : "")}
                    style={{ paddingLeft: 22 + depth * 14 }} onClick={() => { setSelected(u); setEditing(false); }}>
              <span className="uc-id">{u.uc_id}</span> {u.title}
            </button>
          ))}
        </>
      )}
    </div>
  );

  return (
    <div className="ucpage">
      <div className="uc-toolbar">
        <span className="uc-proj" dir="auto" title={projLabel}>{projLabel}</span>
        <span className="reqpill">{count} use {count === 1 ? "case" : "cases"}</span>
        {gen && (() => {
          const done = status?.done ?? 0, tot = status?.total ?? 0;
          const pct = tot ? Math.round((done / tot) * 100) : 0;
          return (
            <span className="uc-progress">
              <span className="uc-bar"><span className="uc-bar-fill" style={{ width: `${pct}%` }} /></span>
              Generating… {pct}% ({done}/{tot}){status?.scope ? ` · ${status.scope}` : ""}
            </span>
          );
        })()}
        <div className="uc-toolbar-sp" />
        {count > 0 && !gen && (
          <button className="linkbtn" onClick={() => newFolder(null)}>+ Folder</button>
        )}
        {count > 0 && !gen && (
          <button className="linkbtn" title="Deleted & superseded use cases (restorable)"
                  onClick={openTrash}>🗑 Trash</button>
        )}
        {count > 0 && !gen && status?.resume?.stale && (
          <button className="primary" title="Requirements changed — regenerate only the affected use cases"
                  onClick={() => runGenerate("sync")}>
            Update ({status.resume.superseded} changed)
          </button>
        )}
        {count > 0 && !gen && status?.resume?.resumable && (
          <button className="primary" title={`${status.resume.pending} batch(es) left`}
                  onClick={() => runGenerate("resume")}>
            Resume ({status.resume.pending} left)
          </button>
        )}
        {count > 0 && (
          <button className="backbtn" disabled={gen}
                  onClick={() => { if (window.confirm("Regenerate replaces all use cases for this BRD, including edits. Continue?")) runGenerate("replace"); }}>
            {gen ? "Generating…" : "Regenerate"}
          </button>
        )}
        {count === 0 && !gen && !loading && (
          <button className="primary" onClick={() => runGenerate("resume")}>Generate use cases</button>
        )}
      </div>

      {warn && (
        <div className="uc-warn">
          ⚠ {warn}
          <button className="linkbtn" onClick={() => setWarn(null)}>Dismiss</button>
        </div>
      )}

      <div className="uc-body">
        <div className="uc-tree" style={{ flexBasis: treeWidth, width: treeWidth }}>
          {loading && <div className="settings-hint">Loading…</div>}
          {!loading && count === 0 && gen && <div className="settings-hint">Generating use cases from this BRD… they’ll appear here as each scope completes.</div>}
          {!loading && count === 0 && !gen && <div className="settings-hint">No use cases yet for this BRD.</div>}
          {folders.map((f) => renderFolder(f, 0))}
        </div>

        <div
          className="resizer uc-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize use-case list"
          title="Drag to resize"
          onMouseDown={onTreeResize}
        />

        <div className="uc-detail">
          {!selected ? (
            <div className="settings-hint">Select a use case to view or edit its details.</div>
          ) : editing ? (
            <>
              <div className="uc-editrow"><label className="uc-elabel">Title</label>
                <input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></div>
              <div className="uc-editrow"><label className="uc-elabel">Description</label>
                <textarea rows={2} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></div>
              <div className="uc-editrow"><label className="uc-elabel">Roles <em>(comma-separated)</em></label>
                <input value={draft.roles} onChange={(e) => setDraft({ ...draft, roles: e.target.value })} /></div>
              <div className="uc-editrow"><label className="uc-elabel">Preconditions</label>
                <textarea rows={2} value={draft.preconditions} onChange={(e) => setDraft({ ...draft, preconditions: e.target.value })} /></div>
              <div className="uc-editrow"><label className="uc-elabel">Steps <em>(one per line)</em></label>
                <textarea rows={5} value={draft.steps} onChange={(e) => setDraft({ ...draft, steps: e.target.value })} /></div>
              <div className="uc-editrow"><label className="uc-elabel">Expected behaviour</label>
                <textarea rows={2} value={draft.expected_behaviour} onChange={(e) => setDraft({ ...draft, expected_behaviour: e.target.value })} /></div>
              <div className="settings-actions">
                <button className="primary" onClick={saveEdit}>Save</button>
                <button className="backbtn" onClick={() => setEditing(false)}>Cancel</button>
              </div>
            </>
          ) : (
            <>
              <div className="uc-detail-head">
                <div className="uc-dtitle"><span className="uc-id">{selected.uc_id}</span> {selected.title}</div>
                <div className="uc-detacts">
                  <button className="linkbtn" onClick={startEdit}>Edit</button>
                  <button className="linkbtn danger" onClick={doDelete}>Delete</button>
                </div>
              </div>
              <div className="uc-field">
                <div className="uc-flabel">Folder</div>
                <select className="settings-select" value={selected.folder_id}
                        onChange={(e) => doMove(Number(e.target.value))}>
                  {flatten(folders).map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
                </select>
              </div>
              {selected.description && <p className="uc-desc">{selected.description}</p>}
              <Field label="Roles">{selected.roles.length ? selected.roles.join(", ") : "—"}</Field>
              <Field label="Preconditions">{selected.preconditions || "—"}</Field>
              <div className="uc-field">
                <div className="uc-flabel">Steps</div>
                {selected.steps.length
                  ? <ol className="uc-steps">{selected.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                  : <div className="uc-fval">—</div>}
              </div>
              <Field label="Expected behaviour">{selected.expected_behaviour || "—"}</Field>
              <div className="uc-field">
                <div className="uc-flabel">Source requirements <em>(traceability)</em></div>
                <div className="uc-cites">
                  {selected.source_chunk_ids.length === 0 && <span className="settings-hint">—</span>}
                  {selected.source_chunk_ids.map((cid) => {
                    const r = reqMap[cid];
                    return <span key={cid} className="uc-cite" title={r?.text || ""}>{r ? (r.req_id || r.section || `#${cid}`) : `#${cid}`}</span>;
                  })}
                </div>
              </div>
              <UseCaseHistory uid={selected.uid} onRestored={load} toast={toast} />
            </>
          )}
        </div>
      </div>

      {trashOpen && (
        <div className="uc-trash-scrim" onClick={() => setTrashOpen(false)}>
          <div className="uc-trash" onClick={(e) => e.stopPropagation()}>
            <div className="uc-trash-head">
              <b>Trash</b>
              <span className="settings-hint">Deleted &amp; superseded use cases — restore any of them.</span>
              <button className="linkbtn" onClick={() => setTrashOpen(false)}>Close</button>
            </div>
            <div className="uc-trash-body">
              {deleted.length === 0 && <div className="settings-hint">Nothing in Trash.</div>}
              {deleted.map((d) => (
                <div key={d.uid} className="uc-trash-row">
                  <span className="uc-id">{d.uc_id || `#${d.uid}`}</span>
                  <span className="uc-trash-title">{d.title || "(untitled)"}</span>
                  <span className={"uc-hist-kind k-" + d.change_kind}>{d.change_kind}</span>
                  <span className="uc-hist-when">{new Date(d.changed_at).toLocaleString()}</span>
                  <button className="linkbtn" onClick={() => restoreFromTrash(d.uid, d.version_no)}>Restore</button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
