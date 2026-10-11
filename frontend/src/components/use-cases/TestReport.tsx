import { useEffect, useMemo, useState } from "react";
import {
  compareCycles, getUseCaseReport, TEST_STATUS_LABEL,
  type Coverage, type RunChange, type RunCompare, type TestCycle, type TestStatus, type UcReport, type UcReportCase,
} from "../../lib/api";
import { reqLabel } from "../../lib/cite";
import { RunProgress } from "./TestCycleBar";

const COVERAGE_ORDER: Coverage[] = ["failed", "blocked", "uncovered", "retest", "partial", "not_run", "passed"];
const COVERAGE_LABEL: Record<Coverage, string> = {
  failed: "Failing", blocked: "Blocked", uncovered: "No test cases", retest: "Passed · needs retest",
  partial: "Partially run", not_run: "Not run", passed: "Passed",
};
const STATUS_COLS: TestStatus[] = ["passed", "failed", "blocked", "in_progress", "not_tested"];

const csvCell = (v: unknown) => {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
function downloadCsv(name: string, header: string[], rows: unknown[][]) {
  // BOM so Excel opens UTF-8 (Arabic requirement text) correctly
  const body = "﻿" + [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([body], { type: "text/csv;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const safeName = (s: string) => s.replace(/[^\w.-]+/g, "_").slice(0, 60);

const RUN_ORDER: RunChange[] = ["regressed", "new_failure", "still_failing", "fixed", "newly_passing", "not_rerun",
  "dropped", "not_run", "still_passing"];
const RUN_LABEL: Record<RunChange, string> = {
  regressed: "Regressed", new_failure: "New failures", still_failing: "Still failing", fixed: "Fixed",
  newly_passing: "Newly passing", not_rerun: "Not re-run", dropped: "Not in this run", not_run: "Not run",
  still_passing: "Still passing",
};
const CHANGE_WORD: Record<string, string> = {
  added: "added", removed: "removed", modified: "modified", requirement_changed: "requirement changed",
  moved: "moved", reordered: "reordered",
};
const st = (s: TestStatus | null) => (s ? TEST_STATUS_LABEL[s] : "—");

// What changed between two runs of the same BRD. The cause column separates a failure on an
// UNCHANGED definition (points at the product) from one after the case / its requirement changed.
function RunComparison({ project, projLabel, current, cycles, onOpenCase }: {
  project: string; projLabel: string; current: TestCycle; cycles: TestCycle[]; onOpenCase: (id: number) => void;
}) {
  const others = cycles.filter((c) => c.id !== current.id);
  const [base, setBase] = useState<number | "">(() => {
    const older = others.filter((c) => c.created_at < current.created_at);
    return older.length ? older[0].id : "";
  });
  const [cmp, setCmp] = useState<RunCompare | null>(null);
  const [showAll, setShowAll] = useState(false);
  useEffect(() => {
    if (base === "") { setCmp(null); return; }
    let live = true;
    compareCycles(project, base, current.id).then((d) => { if (live) setCmp(d.error ? null : d); }).catch(() => {});
    return () => { live = false; };
  }, [project, base, current.id]);
  if (!others.length) return null;
  const quiet = new Set<RunChange>(["still_passing", "not_run"]);
  const items = (cmp?.items || []).filter((i) => showAll || !quiet.has(i.change))
    .sort((a, b) => RUN_ORDER.indexOf(a.change) - RUN_ORDER.indexOf(b.change));
  const cause = (d: string | null, retest: boolean) =>
    d == null ? "" : d === "unchanged" ? "same definition" : `${CHANGE_WORD[d] || d}${retest ? " — test changed" : ""}`;
  const exportCsv = () => cmp && downloadCsv(
    `${safeName(projLabel)}_${safeName(cmp.base.name)}_vs_${safeName(cmp.target.name)}.csv`,
    ["Case", "Title", "Type", cmp.base.name, cmp.target.name, "Change", "Definition", "Defect"],
    cmp.items.map((i) => [i.uc_id, i.title, i.case_type, st(i.base_status), st(i.target_status) + (i.carried ? " (carried)" : ""),
      RUN_LABEL[i.change], cause(i.definition, i.retest), i.defect_ref]));
  return (
    <>
      <h4 className="uc-report-h">Compared with another run
        <select className="settings-select sm" value={base} onChange={(e) => setBase(e.target.value ? Number(e.target.value) : "")}>
          <option value="">Choose a run…</option>
          {others.map((c) => <option key={c.id} value={c.id}>{c.name}{c.version ? ` (v${c.version.number})` : ""}</option>)}
        </select>
        {cmp && <button className="uc-ghostbtn" onClick={exportCsv}>⤓ CSV</button>}
      </h4>
      {cmp && (
        <div className="uc-runcmp">
          <div className="uc-filter">
            {RUN_ORDER.filter((k) => cmp.summary[k]).map((k) => (
              <span key={k} className={"uc-chip rc-" + k}>{RUN_LABEL[k]} <span className="n">{cmp.summary[k]}</span></span>
            ))}
            <label className="uc-optrow"><input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
              show unchanged</label>
          </div>
          {cmp.base.version && cmp.target.version && cmp.base.version.number !== cmp.target.version.number && (
            <div className="settings-hint">Runs on v{cmp.base.version.number} → v{cmp.target.version.number}: the cause column
              tells a failure on the same definition (likely the product) from one after the case or its requirement changed.</div>
          )}
          <div className="uc-rtable">
            {items.map((i) => (
              <button key={i.uid} className={"uc-rcase rc-" + i.change} disabled={i.id == null}
                      onClick={() => i.id != null && onOpenCase(i.id)} title={i.id == null ? "Deleted from the draft" : "Open this use case"}>
                <span className={"uc-rc-kind rc-" + i.change}>{RUN_LABEL[i.change]}</span>
                <span className="uc-id">{i.uc_id}</span> <span dir="auto">{i.title}</span>
                <span className="uc-tsp" />
                <span className="uc-rc-move">{st(i.base_status)} → {st(i.target_status)}{i.carried ? " (carried)" : ""}</span>
                {i.definition && i.definition !== "unchanged" && <span className="uc-rc-cause">{cause(i.definition, i.retest)}</span>}
                {i.defect_ref && <span className="uc-defect">🐞 {i.defect_ref}</span>}
              </button>
            ))}
            {items.length === 0 && <div className="settings-hint">Nothing moved between these runs.</div>}
          </div>
        </div>
      )}
    </>
  );
}
const pct = (x: number | null) =>
  x == null ? "—" : x > 0 && x < 0.01 ? "<1%" : x < 1 && x > 0.99 ? ">99%" : `${Math.round(x * 100)}%`;

// Coverage & traceability report — overall or for one test cycle: KPIs, requirement → cases →
// verdict matrix (worst first), defects, case-type breakdown, CSV export.
export default function TestReport({ project, projLabel, cycles, initialCycleId, onClose, onOpenCase }: {
  project: string;
  projLabel: string;
  cycles: TestCycle[];
  initialCycleId: number | null;
  onClose: () => void;
  onOpenCase: (id: number) => void;
}) {
  const [cycleId, setCycleId] = useState<number | null>(initialCycleId);
  const [rep, setRep] = useState<UcReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [cov, setCov] = useState<Coverage | "all">("all");
  const [open, setOpen] = useState<Set<number>>(new Set());

  useEffect(() => {
    let live = true;
    setLoading(true);
    getUseCaseReport(project, cycleId).then((r) => { if (live) { setRep(r); setLoading(false); } })
      .catch(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [project, cycleId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const byUid = useMemo(() => new Map((rep?.cases || []).map((c) => [c.uid, c])), [rep]);
  const reqs = useMemo(() => {
    const rs = (rep?.requirements || []).filter((r) => cov === "all" || r.coverage === cov);
    return [...rs].sort((a, b) => COVERAGE_ORDER.indexOf(a.coverage) - COVERAGE_ORDER.indexOf(b.coverage)
      || (a.ordinal ?? 0) - (b.ordinal ?? 0));
  }, [rep, cov]);

  const scopeName = rep?.cycle ? rep.cycle.name : "All results";
  const exportCases = () => rep && downloadCsv(`${safeName(projLabel)}_${safeName(scopeName)}_cases.csv`,
    ["Case", "Title", "Type", "Folder", "Status", "Needs retest", "Tested at", "Tested by", "Defect", "Note", "Proofs"],
    rep.cases.map((c) => [c.uc_id, c.title, c.case_type, c.folder, TEST_STATUS_LABEL[c.status], c.needs_retest ? "yes" : "",
      c.tested_at ? new Date(c.tested_at).toLocaleString() : "", c.tested_by, c.defect_ref, c.note, c.proofs]));
  const exportTrace = () => rep && downloadCsv(`${safeName(projLabel)}_${safeName(scopeName)}_traceability.csv`,
    ["Requirement", "Requirement text", "Coverage", "Case", "Case title", "Type", "Status", "Needs retest", "Defect"],
    rep.requirements.flatMap((r) => {
      const label = reqLabel(r);
      const cs = r.case_uids.map((u) => byUid.get(u)).filter(Boolean) as UcReportCase[];
      if (!cs.length) return [[label, r.text, COVERAGE_LABEL[r.coverage], "", "", "", "", "", ""]];
      return cs.map((c) => [label, r.text, COVERAGE_LABEL[r.coverage], c.uc_id, c.title, c.case_type,
        TEST_STATUS_LABEL[c.status], c.needs_retest ? "yes" : "", c.defect_ref]);
    }));

  const toggle = (id: number) => setOpen((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div className="uc-trash-scrim" onClick={onClose}>
      <div className="uc-trash uc-report" onClick={(e) => e.stopPropagation()}>
        <div className="uc-trash-head">
          <b>Test report</b>
          <select className="settings-select sm" value={cycleId ?? ""}
                  onChange={(e) => setCycleId(e.target.value ? Number(e.target.value) : null)}>
            <option value="">All results (latest per case)</option>
            {cycles.map((c) => <option key={c.id} value={c.id}>{c.name}{c.status === "closed" ? " (closed)" : ""}</option>)}
          </select>
          <span className="uc-tsp" />
          <button className="uc-ghostbtn" disabled={!rep} onClick={exportCases}>⤓ Cases CSV</button>
          <button className="uc-ghostbtn" disabled={!rep} onClick={exportTrace}>⤓ Traceability CSV</button>
          <button className="linkbtn" onClick={onClose}>Close</button>
        </div>
        <div className="uc-trash-body uc-report-body">
          {loading && !rep && <div className="settings-hint">Building report…</div>}
          {rep?.error && <div className="settings-hint">{rep.error}</div>}
          {rep && !rep.error && (
            <>
              {rep.cycle?.description && <p className="uc-desc">{rep.cycle.description}</p>}
              <div className="uc-rep-ver">
                {rep.version ? (
                  <span><b>{rep.cycle ? "Tests" : "Latest version"} v{rep.version.number}</b>
                    {rep.version.label ? ` · ${rep.version.label}` : ""}
                    {rep.version.brd_revision_no ? ` · BRD revision ${rep.version.brd_revision_no}` : ""}</span>
                ) : rep.cycle ? <span>Run from before versioning — measured against the current draft</span>
                  : <span>No published version yet</span>}
                {rep.cycle?.outdated && <span className="uc-cyc-old">v{rep.cycle.latest_version} has been published since</span>}
                {!rep.cycle && rep.draft_changes?.has_changes && (
                  <span className="uc-cyc-old">The draft has unpublished changes — publish them to test them in a cycle</span>
                )}
                {!!rep.carried && <span className="settings-hint">{rep.carried} carried pass{rep.carried === 1 ? "" : "es"}</span>}
              </div>
              {rep.changes_since_previous && rep.changes_since_previous.items.length > 0 && (
                <details className="uc-rep-changes">
                  <summary>What changed since v{rep.changes_since_previous.base} —{" "}
                    {(["modified", "requirement_changed", "added", "removed", "moved"] as const)
                      .filter((k) => rep.changes_since_previous!.summary[k])
                      .map((k) => `${rep.changes_since_previous!.summary[k]} ${CHANGE_WORD[k]}`).join(", ")}
                    {rep.changes_since_previous.summary.retest ? ` · ${rep.changes_since_previous.summary.retest} need a fresh run` : ""}
                  </summary>
                  <div className="uc-rtable">
                    {rep.changes_since_previous.items.map((i) => (
                      <div key={i.uid} className="uc-rcase">
                        <span className={"uc-rc-kind ch-" + i.change}>{CHANGE_WORD[i.change] || i.change}</span>
                        <span className="uc-id">{i.uc_id}</span> <span dir="auto">{i.title}</span>
                        {i.fields.length > 0 && <span className="settings-hint">{i.fields.join(", ").replace(/_/g, " ")}</span>}
                        {i.retest && <span className="uc-retest sm">⚠</span>}
                      </div>
                    ))}
                  </div>
                </details>
              )}
              <div className="uc-kpis">
                <div className="uc-kpi"><b>{rep.total}</b><span>cases in scope</span></div>
                <div className="uc-kpi"><b>{pct(rep.progress)}</b><span>run ({rep.executed})</span></div>
                <div className="uc-kpi"><b>{pct(rep.pass_rate)}</b><span>pass rate</span></div>
                <div className="uc-kpi c-failed"><b>{rep.by_status.failed || 0}</b><span>failed</span></div>
                <div className="uc-kpi c-blocked"><b>{rep.by_status.blocked || 0}</b><span>blocked</span></div>
                <div className="uc-kpi c-retest"><b>{rep.needs_retest}</b><span>need retest</span></div>
                <div className="uc-kpi"><b>{rep.out_of_scope}</b><span>out of scope</span></div>
              </div>
              <RunProgress wide s={{ total: rep.total, executed: rep.executed, needs_retest: rep.needs_retest, by_status: rep.by_status }} />

              <h4 className="uc-report-h">Requirement coverage <em>{rep.requirements.length} requirements</em></h4>
              <div className="uc-filter">
                <button className={"uc-chip" + (cov === "all" ? " on" : "")} onClick={() => setCov("all")}>
                  All <span className="n">{rep.requirements.length}</span>
                </button>
                {COVERAGE_ORDER.filter((c) => rep.coverage[c]).map((c) => (
                  <button key={c} className={"uc-chip cov-" + c + (cov === c ? " on" : "")} onClick={() => setCov(c)}>
                    <span className={"dot cov-" + c} />{COVERAGE_LABEL[c]} <span className="n">{rep.coverage[c]}</span>
                  </button>
                ))}
              </div>
              <div className="uc-rtable">
                {reqs.map((r) => {
                  const cs = r.case_uids.map((u) => byUid.get(u)).filter(Boolean) as UcReportCase[];
                  const isOpen = open.has(r.chunk_id);
                  return (
                    <div key={r.chunk_id} className={"uc-rrow" + (isOpen ? " open" : "")}>
                      <button className="uc-rhead" onClick={() => cs.length && toggle(r.chunk_id)} dir="auto">
                        <span className={"uc-cov cov-" + r.coverage}>{COVERAGE_LABEL[r.coverage]}</span>
                        <span className="uc-rlabel">{reqLabel(r)}</span>
                        <span className="uc-rtext">{r.text}
                          {r.changed_since_version && <em className="uc-rflag" title="This is the text the tested version cited; the requirement has been edited since"> · changed since</em>}
                          {r.removed_since_version && <em className="uc-rflag" title="Cited by the tested version; no longer in the BRD"> · removed since</em>}</span>
                        <span className="uc-rcounts">
                          {STATUS_COLS.filter((s) => r.counts[s]).map((s) => (
                            <span key={s} className={"uc-sdot st-" + s} title={`${TEST_STATUS_LABEL[s]}: ${r.counts[s]}`} />
                          ))}
                          {cs.length} case{cs.length === 1 ? "" : "s"}
                        </span>
                      </button>
                      {isOpen && (
                        <div className="uc-rcases">
                          {cs.map((c) => (
                            <button key={c.uid} className="uc-rcase" disabled={c.id == null}
                                    onClick={() => c.id != null && onOpenCase(c.id)}
                                    title={c.id == null ? "Deleted from the draft since this run" : "Open this use case"}>
                              <span className={"uc-tstatus sm st-" + c.status}>{TEST_STATUS_LABEL[c.status]}</span>
                              {c.needs_retest && <span className="uc-retest sm">⚠</span>}
                              {c.carried && <span className="uc-tcarried">carried</span>}
                              <span className="uc-id">{c.uc_id}</span> {c.title}
                              {c.id == null && <em className="uc-rflag"> · deleted since</em>}
                              {c.defect_ref && <span className="uc-defect">🐞 {c.defect_ref}</span>}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
                {reqs.length === 0 && <div className="settings-hint">No requirements in this state.</div>}
              </div>

              {rep.defects.length > 0 && (
                <>
                  <h4 className="uc-report-h">Failures &amp; defects <em>{rep.defects.length}</em></h4>
                  <div className="uc-rtable">
                    {rep.defects.map((d) => (
                      <button key={d.uid} className="uc-rcase" disabled={d.id == null}
                              onClick={() => d.id != null && onOpenCase(d.id)}>
                        <span className={"uc-tstatus sm st-" + d.status}>{TEST_STATUS_LABEL[d.status]}</span>
                        <span className="uc-id">{d.uc_id}</span> {d.title}
                        {d.defect_ref && <span className="uc-defect">🐞 {d.defect_ref}</span>}
                        {d.note && <span className="uc-rnote">{d.note}</span>}
                      </button>
                    ))}
                  </div>
                </>
              )}

              {rep.cycle && <RunComparison project={project} projLabel={projLabel} current={rep.cycle}
                                           cycles={cycles} onOpenCase={onOpenCase} />}

              {Object.keys(rep.by_case_type).length > 0 && (
                <>
                  <h4 className="uc-report-h">By case type</h4>
                  <table className="uc-ttable">
                    <thead><tr><th />{STATUS_COLS.map((s) => <th key={s}>{TEST_STATUS_LABEL[s]}</th>)}</tr></thead>
                    <tbody>
                      {Object.entries(rep.by_case_type).map(([t, m]) => (
                        <tr key={t}><th>{t}</th>{STATUS_COLS.map((s) => <td key={s}>{m[s] || ""}</td>)}</tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
