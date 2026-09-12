import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  generateUseCases, getRequirements, getUseCaseStatus, getUseCases,
  type Requirement, type UseCase, type UseCaseFolder, type UseCaseStatus,
} from "../lib/api";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="uc-field"><div className="uc-flabel">{label}</div><div className="uc-fval">{children}</div></div>;
}

const countAll = (fs: UseCaseFolder[]): number =>
  fs.reduce((n, f) => n + f.use_cases.length + countAll(f.children), 0);

// A dedicated page: use cases derived from one BRD, organized as a folder tree.
// On first open (Option B) it auto-generates in the background if none exist yet,
// filling the tree in as scopes complete; once generated they're stored (instant next time).
export default function UseCasesPage({ project, projLabel, toast }: {
  project: string | null;
  projLabel: string;
  toast: (m: string) => void;
}) {
  const [folders, setFolders] = useState<UseCaseFolder[]>([]);
  const [loading, setLoading] = useState(false);
  const [gen, setGen] = useState(false);
  const [status, setStatus] = useState<UseCaseStatus | null>(null);
  const [selected, setSelected] = useState<UseCase | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [reqMap, setReqMap] = useState<Record<number, Requirement>>({});
  const [warn, setWarn] = useState<string | null>(null);   // persistent notice (partial failure)
  const pollRef = useRef<number | null>(null);
  const autoRef = useRef<string | null>(null);   // project we've already auto-generated for

  const load = useCallback(async (): Promise<number> => {
    if (!project) return 0;
    const d = await getUseCases(project).catch(() => ({ folders: [] as UseCaseFolder[] }));
    const fs = d.folders || [];
    setFolders(fs);
    setExpanded((prev) => (prev.size ? prev : new Set(fs.map((f) => f.id))));  // default-open top level once
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
        if (st?.error) { setWarn(st.error); toast(st.error); }
        else if (failed) {
          const msg = `Generated ${st.made ?? 0} use case(s), but ${failed} batch(es) failed — likely rate limits. Click Regenerate to retry the rest.`;
          setWarn(msg); toast(`${failed} batch(es) failed — see the notice`);
        } else { setWarn(null); toast(`Use cases ready — ${st.made ?? 0} generated ✓`); }
      }
    }, 2000);
  }, [project, load]);  // eslint-disable-line react-hooks/exhaustive-deps

  const runGenerate = useCallback(async (replace: boolean) => {
    if (!project) return;
    setWarn(null);
    const r = await generateUseCases(project, replace);
    if (r.error) { toast(r.error); return; }
    startPolling();
  }, [project, startPolling, toast]);

  useEffect(() => {
    if (!project) return;
    setSelected(null); setStatus(null); setWarn(null); setLoading(true); stopPoll(); setGen(false);
    getRequirements(project)
      .then((rs) => setReqMap(Object.fromEntries(rs.map((r) => [r.chunk_id, r]))))
      .catch(() => setReqMap({}));
    (async () => {
      const st = await getUseCaseStatus(project).catch(() => null);
      await load();
      setLoading(false);
      if (st?.running) { startPolling(); return; }               // a job is already going
      const n = countAll((await getUseCases(project).catch(() => ({ folders: [] as UseCaseFolder[] }))).folders || []);
      if (n === 0 && autoRef.current !== project) {              // Option B: auto-generate once
        autoRef.current = project;
        runGenerate(false);
      }
    })();
    return stopPoll;
  }, [project]);  // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (id: number) =>
    setExpanded((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const count = countAll(folders);

  const renderFolder = (f: UseCaseFolder, depth: number): ReactNode => (
    <div key={f.id} className="uc-folder">
      <button className="uc-foldhead" style={{ paddingLeft: 6 + depth * 14 }} onClick={() => toggle(f.id)}>
        <span className="uc-caret">{expanded.has(f.id) ? "▾" : "▸"}</span>
        <span className="uc-foldname">📁 {f.name}</span>
        <span className="uc-count">{f.use_cases.length + countAll(f.children)}</span>
      </button>
      {expanded.has(f.id) && (
        <>
          {f.children.map((c) => renderFolder(c, depth + 1))}
          {f.use_cases.map((u) => (
            <button key={u.id} className={"uc-leaf" + (selected?.id === u.id ? " sel" : "")}
                    style={{ paddingLeft: 22 + depth * 14 }} onClick={() => setSelected(u)}>
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
        {count > 0 && (
          <button className="backbtn" disabled={gen}
                  onClick={() => { if (window.confirm("Regenerate replaces all use cases for this BRD, including edits. Continue?")) runGenerate(true); }}>
            {gen ? "Generating…" : "Regenerate"}
          </button>
        )}
        {count === 0 && !gen && !loading && (
          <button className="primary" onClick={() => runGenerate(false)}>Generate use cases</button>
        )}
      </div>

      {warn && (
        <div className="uc-warn">
          ⚠ {warn}
          <button className="linkbtn" onClick={() => setWarn(null)}>Dismiss</button>
        </div>
      )}

      <div className="uc-body">
        <div className="uc-tree">
          {loading && <div className="settings-hint">Loading…</div>}
          {!loading && count === 0 && gen && <div className="settings-hint">Generating use cases from this BRD… they’ll appear here as each scope completes.</div>}
          {!loading && count === 0 && !gen && <div className="settings-hint">No use cases yet for this BRD.</div>}
          {folders.map((f) => renderFolder(f, 0))}
        </div>

        <div className="uc-detail">
          {!selected ? (
            <div className="settings-hint">Select a use case to view its details.</div>
          ) : (
            <>
              <div className="uc-dtitle"><span className="uc-id">{selected.uc_id}</span> {selected.title}</div>
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
            </>
          )}
        </div>
      </div>
    </div>
  );
}
