import { useState } from "react";
import {
  createTestCycle, deleteTestCycle, updateTestCycle, TEST_STATUS_LABEL,
  type CycleSource, type TestCycle, type TestStatus, type UseCaseFolder,
} from "../../lib/api";

export interface RunStats { total: number; executed: number; needs_retest: number; by_status: Partial<Record<TestStatus, number>> }

const SEG_ORDER: TestStatus[] = ["passed", "failed", "blocked", "in_progress"];

// Segmented run progress: one bar, one segment per verdict, the rest is "not run".
export function RunProgress({ s, wide }: { s: RunStats; wide?: boolean }) {
  const pct = (n: number) => (s.total ? (n / s.total) * 100 : 0);
  const title = SEG_ORDER.map((k) => `${TEST_STATUS_LABEL[k]} ${s.by_status[k] || 0}`).join(" · ")
    + ` · Not run ${s.by_status.not_tested || 0}` + (s.needs_retest ? ` · ⚠ ${s.needs_retest} need retest` : "");
  return (
    <span className={"uc-run" + (wide ? " wide" : "")} title={title}>
      <span className="uc-run-bar">
        {SEG_ORDER.map((k) => (s.by_status[k] ? <span key={k} className={"seg st-" + k} style={{ width: `${pct(s.by_status[k] || 0)}%` }} /> : null))}
      </span>
      <span className="uc-run-txt">
        {s.executed}/{s.total} run
        {s.by_status.passed ? <> · <b className="c-passed">{s.by_status.passed}✓</b></> : null}
        {s.by_status.failed ? <> · <b className="c-failed">{s.by_status.failed}✕</b></> : null}
        {s.by_status.blocked ? <> · <b className="c-blocked">{s.by_status.blocked}⏸</b></> : null}
        {s.needs_retest ? <> · <b className="c-retest">⚠{s.needs_retest}</b></> : null}
      </span>
    </span>
  );
}

const SOURCES: { v: CycleSource; label: string; hint: string }[] = [
  { v: "all", label: "All in-scope cases", hint: "A full run of everything in scope" },
  { v: "not_passed", label: "Not yet passed", hint: "Everything that hasn't passed" },
  { v: "failed_blocked", label: "Failed or blocked", hint: "A regression re-run of what broke" },
  { v: "retest", label: "Needs retest", hint: "Verdicts made stale by a definition change" },
  { v: "empty", label: "Start empty", hint: "Cases join as you record results in it" },
];

const flat = (fs: UseCaseFolder[], d = 0): { id: number; label: string; depth: number }[] =>
  fs.flatMap((f) => [{ id: f.id, label: f.name, depth: d }, ...flat(f.children, d + 1)]);

function CycleCreate({ project, cycles, folders, onClose, onCreated, toast }: {
  project: string; cycles: TestCycle[]; folders: UseCaseFolder[];
  onClose: () => void; onCreated: (c: TestCycle) => void; toast: (m: string) => void;
}) {
  const [name, setName] = useState(() => `Run ${new Date().toLocaleDateString()}`);
  const [description, setDescription] = useState("");
  const [source, setSource] = useState<CycleSource>("all");
  const [fromCycle, setFromCycle] = useState<number | "">("");
  const [types, setTypes] = useState<Set<string>>(new Set());
  const [fids, setFids] = useState<Set<number>>(new Set());
  const [carry, setCarry] = useState(true);
  const [busy, setBusy] = useState(false);
  const canFrom = source === "not_passed" || source === "failed_blocked" || source === "all";

  const toggle = <T,>(set: Set<T>, v: T) => { const n = new Set(set); n.has(v) ? n.delete(v) : n.add(v); return n; };

  const create = async () => {
    if (!name.trim()) { toast("Give the cycle a name"); return; }
    setBusy(true);
    const r = await createTestCycle({
      project, name: name.trim(), description: description.trim() || undefined, source,
      case_types: types.size ? [...types] : undefined, folder_ids: fids.size ? [...fids] : undefined,
      from_cycle_id: canFrom && fromCycle !== "" ? fromCycle : null, carry,
    });
    setBusy(false);
    if (r.error || !r.cycle) { toast(r.error || "Could not create cycle"); return; }
    const c = r.cycle, n = c.stats.total;
    toast(`Cycle “${c.name}” on v${c.version?.number ?? "?"} — ${n} case${n === 1 ? "" : "s"} planned`
      + (c.carried ? `, ${c.carried} pass${c.carried === 1 ? "" : "es"} carried over` : "")
      + (c.unpublished_excluded ? ` · ${c.unpublished_excluded} unpublished case${c.unpublished_excluded === 1 ? "" : "s"} left out — publish to include` : ""));
    onCreated(r.cycle);
  };

  return (
    <div className="uc-trash-scrim" onClick={onClose}>
      <div className="uc-trash uc-cyc-modal" onClick={(e) => e.stopPropagation()}>
        <div className="uc-trash-head">
          <b>New test cycle</b>
          <span className="settings-hint">A named run of the latest published version — its results say exactly what was tested.</span>
          <button className="linkbtn" onClick={onClose}>Close</button>
        </div>
        <div className="uc-trash-body uc-cyc-form">
          <label className="uc-elabel">Name</label>
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") create(); }} />
          <label className="uc-elabel">Description <em>(optional)</em></label>
          <input value={description} placeholder="Build, environment, goal…" onChange={(e) => setDescription(e.target.value)} />
          <label className="uc-elabel">Plan which cases</label>
          <div className="uc-cyc-sources">
            {SOURCES.map((s) => (
              <label key={s.v} className={"uc-cyc-src" + (source === s.v ? " on" : "")}>
                <input type="radio" name="src" checked={source === s.v} onChange={() => setSource(s.v)} />
                <span><b>{s.label}</b><em>{s.hint}</em></span>
              </label>
            ))}
          </div>
          {source !== "empty" && (
            <>
              {canFrom && cycles.length > 0 && (
                <>
                  <label className="uc-elabel">Based on <em>(optional — narrow to a previous cycle's cases and results)</em></label>
                  <select className="settings-select" value={fromCycle}
                          onChange={(e) => setFromCycle(e.target.value ? Number(e.target.value) : "")}>
                    <option value="">Latest results overall</option>
                    {cycles.map((c) => <option key={c.id} value={c.id}>{c.name}{c.status === "closed" ? " (closed)" : ""}</option>)}
                  </select>
                </>
              )}
              <label className="uc-elabel">Case types <em>(none ticked = all)</em></label>
              <div className="uc-filter">
                {["functional", "negative", "edge"].map((t) => (
                  <button key={t} className={"uc-chip ct-" + t + (types.has(t) ? " on" : "")}
                          onClick={() => setTypes((s) => toggle(s, t))}>
                    <span className={"dot ct-" + t} />{t[0].toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>
              <label className="uc-elabel">Folders <em>(none ticked = all; sub-folders included)</em></label>
              <div className="uc-cyc-folders">
                {flat(folders).map((f) => (
                  <label key={f.id} style={{ paddingInlineStart: 6 + f.depth * 14 }}>
                    <input type="checkbox" checked={fids.has(f.id)} onChange={() => setFids((s) => toggle(s, f.id))} /> 📁 {f.label}
                  </label>
                ))}
              </div>
            </>
          )}
          {source !== "empty" && (
            <label className="uc-optrow uc-cyc-carry">
              <input type="checkbox" checked={carry} onChange={(e) => setCarry(e.target.checked)} />
              <span><b>Carry over passes of unchanged cases</b>
                <em>A case that passed and whose steps and cited requirements haven't changed since starts as
                  Passed (marked carried). Untick for a full regression — everything starts Not run.</em></span>
            </label>
          )}
          <div className="settings-actions">
            <button className="uc-compose-send on" disabled={busy} onClick={create}>{busy ? "Creating…" : "Create cycle"}</button>
            <button className="backbtn" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// Toolbar control: pick the active test cycle (verdicts are recorded into it and the tree shows
// its statuses), see its live progress, open the report, and manage cycles.
export default function TestCycleBar({ project, cycles, active, stats, folders, onSelect, onChanged, onReport,
  toast, confirm, askPrompt }: {
  project: string;
  cycles: TestCycle[];
  active: TestCycle | null;
  stats: RunStats;
  folders: UseCaseFolder[];
  onSelect: (id: number | null) => void;
  onChanged: (select?: number | null) => Promise<void>;
  onReport: () => void;
  toast: (m: string) => void;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;
  askPrompt: (title: string, def: string, okLabel: string, placeholder?: string) => Promise<string | null>;
}) {
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);

  const rename = async () => {
    if (!active) return;
    const name = await askPrompt("Rename test cycle", active.name, "Rename", "Cycle name");
    if (!name || name === active.name) return;
    const r = await updateTestCycle(active.id, { name });
    if (r.error) { toast(r.error); return; }
    await onChanged();
  };
  const setStatus = async (status: "open" | "closed") => {
    if (!active) return;
    if (status === "closed" && !(await confirm(
      `Close “${active.name}”? Its results become read-only (you can reopen it later).`, "Close cycle"))) return;
    const r = await updateTestCycle(active.id, { status });
    if (r.error) { toast(r.error); return; }
    toast(status === "closed" ? "Cycle closed 🔒" : "Cycle reopened ✓");
    await onChanged();
  };
  const remove = async () => {
    if (!active) return;
    if (!(await confirm(
      `Delete test cycle “${active.name}”? Its recorded results and evidence are kept (as ad-hoc runs).`, "Delete cycle"))) return;
    const r = await deleteTestCycle(active.id);
    if (r.error) { toast(r.error); return; }
    await onChanged(null);
  };

  return (
    <div className="uc-cyc">
      <div className="uc-stdrop">
        <button className={"uc-cyc-btn" + (active ? " on" : "")} onClick={() => setOpen((o) => !o)}
                title="Test cycle — verdicts are recorded into the active cycle">
          <span className="uc-cyc-ico">{active?.status === "closed" ? "🔒" : "🧪"}</span>
          <span className="uc-cyc-name">{active ? active.name : "All results"}</span>
          {active?.version && <span className="uc-cyc-ver" title="The published version this cycle tests">v{active.version.number}</span>}
          {active?.outdated && <span className="uc-cyc-old" title={`v${active.latest_version} has been published since — finish this run, then start a new cycle to test v${active.latest_version}`}>⚠ v{active.latest_version}</span>}
          <span className="uc-caret">▾</span>
        </button>
        {open && (
          <>
            <div className="uc-stdrop-backdrop" onClick={() => setOpen(false)} />
            <div className="uc-stdrop-menu uc-cyc-menu" role="listbox">
              <button className={"uc-cyc-item" + (!active ? " on" : "")} onClick={() => { onSelect(null); setOpen(false); }}>
                <b>All results</b><em>Latest verdict per case, across every run</em>
              </button>
              {cycles.length > 0 && <div className="uc-cyc-sep">Test cycles</div>}
              {cycles.map((c) => (
                <button key={c.id} className={"uc-cyc-item" + (active?.id === c.id ? " on" : "")}
                        onClick={() => { onSelect(c.id); setOpen(false); }}>
                  <b>{c.status === "closed" ? "🔒 " : ""}{c.name}
                    {c.version && <span className="uc-cyc-ver">v{c.version.number}</span>}
                    {c.outdated && <span className="uc-cyc-old">v{c.latest_version} available</span>}</b>
                  <RunProgress s={c.stats} />
                </button>
              ))}
              <div className="uc-cyc-sep" />
              <button className="uc-cyc-item act" onClick={() => { setCreating(true); setOpen(false); }}>＋ New test cycle…</button>
              {active && (
                <>
                  <button className="uc-cyc-item act" onClick={() => { setOpen(false); rename(); }}>✎ Rename “{active.name}”…</button>
                  {active.status === "open"
                    ? <button className="uc-cyc-item act" onClick={() => { setOpen(false); setStatus("closed"); }}>🔒 Close cycle</button>
                    : <button className="uc-cyc-item act" onClick={() => { setOpen(false); setStatus("open"); }}>↺ Reopen cycle</button>}
                  <button className="uc-cyc-item act danger" onClick={() => { setOpen(false); remove(); }}>✕ Delete cycle…</button>
                </>
              )}
            </div>
          </>
        )}
      </div>
      {stats.total > 0 && <RunProgress s={stats} />}
      <button className="uc-tbbtn" onClick={onReport} title="Coverage & traceability report">📊 Report</button>
      {creating && (
        <CycleCreate project={project} cycles={cycles} folders={folders} toast={toast}
                     onClose={() => setCreating(false)}
                     onCreated={async (c) => { setCreating(false); await onChanged(c.id); }} />
      )}
    </div>
  );
}
