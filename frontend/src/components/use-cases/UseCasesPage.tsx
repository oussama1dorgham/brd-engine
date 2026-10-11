import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  createUcFolder, deleteUcFolder, deleteUseCase, generateUseCases, getRequirements,
  getUseCaseStatus, getUseCases, moveUseCase, renameUcFolder, updateUseCase,
  getCycleStatuses, getTestCycles, recordTestBulk, setCycleMembers, setScopeBulk,
  TEST_STATUS_LABEL,
  type CycleCase, type Requirement, type TestCycle, type TestStatus, type UcCardState,
  type UseCase, type UseCaseFolder, type UseCaseStatus,
} from "../../lib/api";
import { useResizable } from "../../lib/useResizable";
import UseCaseAudit, { ago } from "./UseCaseAudit";
import UseCaseTesting, { isTypingTarget } from "./UseCaseTesting";
import TestCycleBar, { RunProgress, type RunStats } from "./TestCycleBar";
import TestReport from "./TestReport";
import VersionBar from "./VersionBar";
import RequirementEvidence from "../brd/RequirementEvidence";
import { getDeletedUseCases, getRequirementTitles, restoreUseCase, type UcDeleted } from "../../lib/api";
import { reqLabel, snippet as citeSnippet } from "../../lib/cite";

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

// Patch cards in the tree by id (optimistic / server-confirmed state, no reload).
const patchTree = (fs: UseCaseFolder[], patches: Map<number, Partial<UseCase>>): UseCaseFolder[] =>
  fs.map((f) => ({ ...f, children: patchTree(f.children, patches),
                   use_cases: f.use_cases.map((u) => (patches.has(u.id) ? { ...u, ...patches.get(u.id) } : u)) }));
const cardPatch = (c: UcCardState): Partial<UseCase> => ({
  test_status: c.test_status, in_scope: c.in_scope, needs_retest: c.needs_retest,
  ...(c.last_at ? { last_by: c.last_by ?? null, last_at: c.last_at, last_summary: c.last_summary ?? null } : {}),
});
type DetailTab = "overview" | "run" | "audit";

const flatten = (fs: UseCaseFolder[], depth = 0): { id: number; label: string }[] =>
  fs.flatMap((f) => [{ id: f.id, label: `${"  ".repeat(depth)}${f.name}` }, ...flatten(f.children, depth + 1)]);
// cards in render order (a folder's sub-folders first, then its cards — as renderFolder draws them)
const cardsInOrder = (fs: UseCaseFolder[]): UseCase[] =>
  fs.flatMap((f) => [...cardsInOrder(f.children), ...f.use_cases]);
const parentMap = (fs: UseCaseFolder[], acc = new Map<number, number | null>()) => {
  for (const f of fs) { acc.set(f.id, f.parent_id); parentMap(f.children, acc); }
  return acc;
};

// Test-case angles: functional (happy path) / negative (invalid, error) / edge (boundary).
const CTYPES = ["functional", "negative", "edge"] as const;
type CType = typeof CTYPES[number];
const CTYPE_LABEL: Record<CType, string> = { functional: "Functional", negative: "Negative", edge: "Edge" };

// Keep only cards matching `pred`, dropping folders left with nothing under them.
const filterTreeBy = (fs: UseCaseFolder[], pred: (u: UseCase) => boolean): UseCaseFolder[] =>
  fs.map((f) => ({ ...f, children: filterTreeBy(f.children, pred),
                   use_cases: f.use_cases.filter(pred) }))
    .filter((f) => f.use_cases.length > 0 || f.children.length > 0);

const TSTATUS_ORDER: TestStatus[] = ["passed", "failed", "blocked", "in_progress", "not_tested"];
const BULK_VERDICTS: TestStatus[] = ["passed", "failed", "blocked", "in_progress", "not_tested"];
type StatusFilter = TestStatus | "all" | "retest" | "oos";

const lsGet = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k: string, v: string | null) => {
  try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* non-critical */ }
};

// A dedicated page: use cases derived from one BRD, organized as a folder tree.
// On first open (Option B) it auto-generates in the background if none exist yet,
// filling the tree in as scopes complete; once generated they're stored (instant next time).
// Test execution runs inside TEST CYCLES: the active cycle's statuses overlay the tree and
// verdicts are recorded into it (see docs/USECASE_TESTING.md).
export default function UseCasesPage({ project, projLabel, toast, confirm, askPrompt, model }: {
  project: string | null;
  projLabel: string;
  toast: (m: string) => void;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;   // app confirm modal (not window.confirm)
  askPrompt: (title: string, def: string, okLabel: string, placeholder?: string) => Promise<string | null>;  // app prompt (not window.prompt)
  model: string | null;   // generation model selected in the chat; used for generation too
}) {
  const [folders, setFolders] = useState<UseCaseFolder[]>([]);
  const [loading, setLoading] = useState(false);
  const [gen, setGen] = useState(false);
  const [status, setStatus] = useState<UseCaseStatus | null>(null);
  const [selected, setSelected] = useState<UseCase | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [reqMap, setReqMap] = useState<Record<number, Requirement>>({});
  const [titles, setTitles] = useState<Record<number, string>>({});   // cached friendly citation labels
  const [evCid, setEvCid] = useState<number | null>(null);            // which cited requirement's evidence is open
  const [warn, setWarn] = useState<string | null>(null);   // persistent notice (partial failure)
  const [trashOpen, setTrashOpen] = useState(false);       // Trash view (deleted/superseded cards)
  const [deleted, setDeleted] = useState<UcDeleted[]>([]);
  const [coverage, setCoverage] = useState<"functional" | "full">(() =>   // generation depth (persisted)
    lsGet("brd.ucCoverage") === "full" ? "full" : "functional");
  // view filters (one popover) + run options
  const [typeFilter, setTypeFilter] = useState<CType | "all">("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [cycleOnly, setCycleOnly] = useState(true);                 // with a cycle active: only its planned cases
  const [filterOpen, setFilterOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [autoAdvance, setAutoAdvance] = useState(() => lsGet("brd.ucAutoAdvance") === "1");
  // test cycles
  const [cycles, setCycles] = useState<TestCycle[]>([]);
  const [activeCycleId, setActiveCycleId] = useState<number | null>(null);
  const [cycleCases, setCycleCases] = useState<Record<string, CycleCase>>({});
  const [reportOpen, setReportOpen] = useState(false);
  // detail workspace: tabs + focus mode (tree hidden) + the run script's step checklist
  const [tab, setTab] = useState<DetailTab>(() => {
    const t = lsGet("brd.ucTab"); return t === "run" || t === "audit" ? t : "overview";
  });
  const [focus, setFocus] = useState(() => lsGet("brd.ucFocus") === "1");
  const [stepMarks, setStepMarks] = useState<Record<number, "ok" | "fail">>({});
  const [runReq, setRunReq] = useState<{ status: TestStatus; note?: string; nonce: number } | null>(null);
  const [auditKey, setAuditKey] = useState(0);
  const chooseTab = (t: DetailTab) => { setTab(t); lsSet("brd.ucTab", t); };
  const toggleFocus = () => setFocus((f) => { lsSet("brd.ucFocus", f ? "0" : "1"); return !f; });
  // multi-select for bulk actions (Ctrl/⌘-click, Shift-click range)
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const lastPickRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);
  const autoRef = useRef<string | null>(null);   // project we've already auto-generated for
  const sigRef = useRef<string>("");              // last-rendered tree signature (skip no-op re-renders)
  const cyclesTimer = useRef<number | null>(null);

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

  // --- test cycles ---
  const loadCycles = useCallback(async (select?: number | null) => {
    if (!project) return;
    const cs = await getTestCycles(project).catch(() => [] as TestCycle[]);
    setCycles(cs);
    setActiveCycleId((cur) => {
      const want = select !== undefined ? select : cur;
      return want != null && cs.some((c) => c.id === want) ? want : null;
    });
  }, [project]);
  // cycle list stats (menu) refresh lazily after a burst of verdicts
  const refreshCyclesSoon = () => {
    if (cyclesTimer.current) window.clearTimeout(cyclesTimer.current);
    cyclesTimer.current = window.setTimeout(() => loadCycles(), 1500);
  };
  const loadCycleCases = useCallback(async (id: number | null) => {
    if (id == null) { setCycleCases({}); return; }
    const d = await getCycleStatuses(id).catch(() => null);
    setCycleCases(d?.cases || {});
  }, []);
  useEffect(() => {
    if (project) lsSet(`brd.ucCycle.${project}`, activeCycleId == null ? null : String(activeCycleId));
    loadCycleCases(activeCycleId);
  }, [activeCycleId]);  // eslint-disable-line react-hooks/exhaustive-deps

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
    const r = await generateUseCases(project, mode === "replace", model, mode, coverage);
    if (r.error) { setGen(false); toast(r.error); return; }
    startPolling();
  }, [project, startPolling, toast, model, coverage]);

  const chooseCoverage = (c: "functional" | "full") => { setCoverage(c); lsSet("brd.ucCoverage", c); };

  useEffect(() => {
    if (!project) return;
    sigRef.current = "";   // force the first load for this project to commit
    setSelected(null); setStatus(null); setWarn(null); setLoading(true); stopPoll(); setGen(false);
    setPicked(new Set()); setCycleCases({}); setCycles([]);
    const saved = Number(lsGet(`brd.ucCycle.${project}`));
    loadCycles(Number.isFinite(saved) && saved > 0 ? saved : null);
    // include archived requirements so a use case that cites one shows its real text + a
    // "removed from the BRD" tag instead of a generic "Requirement" placeholder (archived
    // chunks keep resolving — they're never hard-deleted).
    getRequirements(project, true)
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

  useEffect(() => { setEvCid(null); setStepMarks({}); setRunReq(null); }, [selected?.id]);   // per-card state resets

  // Fetch friendly titles for the selected use case's cited requirements (cached backend-side;
  // tags fall back to the Phase-1 label until these arrive).
  useEffect(() => {
    if (!project || !selected || selected.source_chunk_ids.length === 0) return;
    const need = selected.source_chunk_ids.filter((c) => !(c in titles));
    if (need.length === 0) return;
    let cancelled = false;
    getRequirementTitles(project, need).then((t) => {
      if (cancelled) return;
      const add: Record<number, string> = {};
      for (const k in t) if (t[k]) add[Number(k)] = t[k];
      if (Object.keys(add).length) setTitles((prev) => ({ ...prev, ...add }));
    }).catch(() => { /* fail-open: keep fallback labels */ });
    return () => { cancelled = true; };
  }, [selected, project]);  // eslint-disable-line react-hooks/exhaustive-deps

  // --- editing (QA) ---
  const [editing, setEditing] = useState(false);
  const [histKey, setHistKey] = useState(0);   // bumped on edit/move so the History panel refetches
  const [draft, setDraft] = useState({ title: "", description: "", roles: "", preconditions: "", steps: "", expected_behaviour: "", case_type: "functional" as CType });

  const startEdit = () => {
    if (!selected) return;
    setDraft({
      title: selected.title, description: selected.description, roles: selected.roles.join(", "),
      preconditions: selected.preconditions, steps: selected.steps.join("\n"),
      expected_behaviour: selected.expected_behaviour, case_type: selected.case_type,
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
      case_type: draft.case_type,
    });
    if (r.error) { toast(r.error); return; }
    setEditing(false); await load(); setHistKey((k) => k + 1); setAuditKey((k) => k + 1); toast("Saved ✓");
    if (activeCycleId != null) loadCycleCases(activeCycleId);   // the edit may have made a verdict stale
  };
  const doDelete = async () => {
    if (!selected) return;
    const ok = await confirm(
      `Delete “${selected.uc_id || selected.title}”? It stays recoverable from Trash.`,
      "Delete use case",
    );
    if (!ok) return;
    const r = await deleteUseCase(selected.id);
    if (r.error) { toast(r.error); return; }
    setSelected(null); setEditing(false); await load(); toast("Use case deleted");
  };
  const doMove = async (folderId: number) => {
    if (!selected) return;
    const r = await moveUseCase(selected.id, folderId);
    if (r.error) { toast(r.error); return; }
    await load(); setHistKey((k) => k + 1); setAuditKey((k) => k + 1); toast("Moved ✓");
  };
  const newFolder = async (parentId: number | null) => {
    if (!project) return;
    const name = await askPrompt(parentId ? "New sub-folder" : "New folder", "", "Create", "Folder name");
    if (!name) return;
    const r = await createUcFolder(project, parentId, name);
    if (r.error) { toast(r.error); return; }
    if (parentId) setExpanded((s) => new Set(s).add(parentId));
    await load();
  };
  const renameFolder = async (f: UseCaseFolder) => {
    const name = await askPrompt("Rename folder", f.name, "Rename", "Folder name");
    if (!name || name === f.name) return;
    const r = await renameUcFolder(f.id, name);
    if (r.error) { toast(r.error); return; }
    await load();
  };
  const delFolder = async (f: UseCaseFolder) => {
    const cnt = f.use_cases.length + countAll(f.children);
    const ok = await confirm(
      `Delete folder “${f.name}”${cnt ? ` and its ${cnt} use case(s)` : ""}?${cnt ? " The use cases stay recoverable from Trash." : ""}`,
      "Delete folder",
    );
    if (!ok) return;
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

  // --- derived view: the active cycle's statuses overlay the tree ---
  const activeCycle = cycles.find((c) => c.id === activeCycleId) || null;
  const eff = useCallback((u: UseCase) => {
    if (!activeCycle) return { status: u.test_status, retest: u.needs_retest, member: true };
    const c = cycleCases[String(u.uid)];
    return { status: (c?.status ?? "not_tested") as TestStatus, retest: !!c?.needs_retest, member: !!c };
  }, [activeCycle, cycleCases]);

  const allCards = useMemo(() => cardsInOrder(folders), [folders]);
  const count = allCards.length;
  const typeCounts = useMemo(() => {
    const acc: Record<string, number> = { functional: 0, negative: 0, edge: 0 };
    for (const u of allCards) acc[u.case_type] = (acc[u.case_type] || 0) + 1;
    return acc;
  }, [allCards]);
  const hasMixedTypes = count > 0 && (typeCounts.negative > 0 || typeCounts.edge > 0);
  const scoped = (u: UseCase) => !activeCycle || !cycleOnly || eff(u).member;   // the cycle's planned set
  const stats: RunStats = useMemo(() => {
    const s: RunStats = { total: 0, executed: 0, needs_retest: 0, by_status: {} };
    for (const u of allCards) {
      const e = eff(u);
      if (!u.in_scope || (activeCycle && !e.member)) continue;
      s.total++;
      s.by_status[e.status] = (s.by_status[e.status] || 0) + 1;
      if (e.status !== "not_tested") s.executed++;
      if (e.retest) s.needs_retest++;
    }
    return s;
  }, [allCards, eff, activeCycle]);
  const statusCounts: Record<string, number> = {};
  let retestCount = 0, oosCount = 0;
  for (const u of allCards) {
    if (!scoped(u)) continue;
    const e = eff(u);
    statusCounts[e.status] = (statusCounts[e.status] || 0) + 1;
    if (e.retest) retestCount++;
    if (!u.in_scope) oosCount++;
  }
  const matchStatus = (u: UseCase) => {
    const e = eff(u);
    return statusFilter === "all" ? true
      : statusFilter === "retest" ? e.retest
      : statusFilter === "oos" ? !u.in_scope
      : e.status === statusFilter;
  };
  const activeFilters = (typeFilter !== "all" ? 1 : 0) + (statusFilter !== "all" ? 1 : 0);
  const visibleFolders = (typeFilter === "all" && statusFilter === "all" && !(activeCycle && cycleOnly)) ? folders
    : filterTreeBy(folders, (u) => (typeFilter === "all" || u.case_type === typeFilter) && matchStatus(u) && scoped(u));
  const visibleCards = useMemo(() => cardsInOrder(visibleFolders), [visibleFolders]);
  const parents = useMemo(() => parentMap(folders), [folders]);

  // Select a card from code (keyboard / report / auto-advance): expand its folders + scroll to it.
  const focusCard = useCallback((u: UseCase) => {
    setSelected(u); setEditing(false);
    setExpanded((s) => {
      const n = new Set(s);
      for (let f: number | null | undefined = u.folder_id; f != null; f = parents.get(f)) n.add(f);
      return n;
    });
    window.setTimeout(() => document.querySelector(`[data-ucid="${u.id}"]`)?.scrollIntoView({ block: "nearest" }), 30);
  }, [parents]);

  const nextNotRun = useCallback((fromId: number | null) => {
    const list = visibleCards.filter((u) => u.in_scope);
    const start = fromId == null ? -1 : list.findIndex((u) => u.id === fromId);
    for (let k = 1; k <= list.length; k++) {
      const u = list[(start + k + list.length) % list.length];
      if (u && u.id !== fromId && eff(u).status === "not_tested") return u;
    }
    return null;
  }, [visibleCards, eff]);

  // A write's card state → tree row + detail panel + cycle overlay; optionally auto-advance.
  const onCard = (c: UcCardState, kind: "status" | "note" | "scope" | "undo") => {
    setFolders((fs) => patchTree(fs, new Map([[c.id, cardPatch(c)]])));
    setSelected((s) => (s && s.id === c.id ? { ...s, ...cardPatch(c) } : s));
    if (c.cycle && activeCycleId != null && c.cycle.cycle_id === activeCycleId) {
      const cc = c.cycle;
      setCycleCases((m) => (cc.in_cycle
        ? { ...m, [String(c.uid)]: { ...(m[String(c.uid)] || { tested_at: null, defect_ref: null }), status: cc.status, needs_retest: cc.needs_retest } }
        : m));
    }
    if (activeCycleId != null) refreshCyclesSoon();
    setAuditKey((k) => k + 1);
    if (kind === "status" && autoAdvance) {
      const nxt = nextNotRun(c.id);
      if (nxt) window.setTimeout(() => focusCard(nxt), 350);
      else toast("That was the last case not yet run 🎉");
    }
  };

  // --- multi-select + bulk ---
  const onLeafClick = (e: React.MouseEvent, u: UseCase) => {
    if (e.ctrlKey || e.metaKey) {
      setPicked((s) => { const n = new Set(s); n.has(u.id) ? n.delete(u.id) : n.add(u.id); return n; });
      lastPickRef.current = u.id;
      return;
    }
    if (e.shiftKey && lastPickRef.current != null) {
      const a = visibleCards.findIndex((x) => x.id === lastPickRef.current);
      const b = visibleCards.findIndex((x) => x.id === u.id);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        setPicked((s) => new Set([...s, ...visibleCards.slice(lo, hi + 1).map((x) => x.id)]));
        return;
      }
    }
    setPicked(new Set());
    lastPickRef.current = u.id;
    // opening a case from the list starts on Overview (understand it before running it);
    // run-flow navigation (N / J / K / Next not run / auto-advance) keeps the current tab
    if (selected?.id !== u.id) chooseTab("overview");
    setSelected(u); setEditing(false);
  };
  const pickFolder = (f: UseCaseFolder) => {
    const ids = cardsInOrder([f]).map((u) => u.id);
    setPicked((s) => new Set([...s, ...ids]));
  };
  const applyCards = (cards: UcCardState[]) => {
    setFolders((fs) => patchTree(fs, new Map(cards.map((c) => [c.id, cardPatch(c)]))));
    setSelected((s) => { const c = s && cards.find((x) => x.id === s.id); return s && c ? { ...s, ...cardPatch(c) } : s; });
    const cyc = cards.filter((c) => c.cycle?.in_cycle);
    if (cyc.length) setCycleCases((m) => {
      const n = { ...m };
      for (const c of cyc) n[String(c.uid)] = { ...(n[String(c.uid)] || { tested_at: null, defect_ref: null }), status: c.cycle!.status, needs_retest: c.cycle!.needs_retest };
      return n;
    });
    setHistKey((k) => k + 1);   // the open card's timeline refetches
    setAuditKey((k) => k + 1);
  };
  const bulkMark = async (s: TestStatus) => {
    const ids = [...picked];
    if (activeCycle?.status === "closed") { toast("This test cycle is closed — reopen it to record results"); return; }
    if (ids.length > 20 && !(await confirm(`Mark ${ids.length} use cases as ${TEST_STATUS_LABEL[s]}?`, "Mark all"))) return;
    const r = await recordTestBulk(ids, s, activeCycleId);
    if (r.error) { toast(r.error); return; }
    applyCards(r.cards || []);
    if (activeCycleId != null) refreshCyclesSoon();
    toast(`${r.cards?.length || 0} marked ${TEST_STATUS_LABEL[s]} ✓`);
  };
  const bulkScope = async (inScope: boolean) => {
    const r = await setScopeBulk([...picked], inScope);
    if (r.error) { toast(r.error); return; }
    applyCards(r.cards || []);
    toast(`${r.cards?.length || 0} ${inScope ? "returned to scope" : "marked out of scope"}`);
  };
  const bulkCycle = async (add: boolean) => {
    if (activeCycleId == null) return;
    const ids = [...picked];
    const uids = allCards.filter((u) => picked.has(u.id)).map((u) => u.uid);
    const r = await setCycleMembers(activeCycleId, add ? ids : [], add ? [] : uids);
    if (r.error) { toast(r.error); return; }
    await loadCycleCases(activeCycleId);
    refreshCyclesSoon();
    toast(add ? `${ids.length} planned into “${activeCycle?.name}”` : `${ids.length} removed from “${activeCycle?.name}”`);
  };

  // --- keyboard navigation: J/K move, N next not run, Esc clears the selection set ---
  const modalOpen = trashOpen || reportOpen || filterOpen || manageOpen;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (modalOpen || editing || e.ctrlKey || e.metaKey || e.altKey || isTypingTarget(e.target)) return;
      const k = e.key.toLowerCase();
      if (k === "escape" && picked.size) { setPicked(new Set()); return; }
      if (k !== "j" && k !== "k" && k !== "n") return;
      e.preventDefault();
      if (k === "n") {
        const u = nextNotRun(selected?.id ?? null);
        if (u) focusCard(u); else toast("No cases left to run here 🎉");
        return;
      }
      const i = selected ? visibleCards.findIndex((u) => u.id === selected.id) : -1;
      const u = visibleCards[k === "j" ? Math.min(visibleCards.length - 1, i + 1) : Math.max(0, i - 1)];
      if (u) focusCard(u);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

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
          <button title="Select all cases in this folder (for bulk actions)" onClick={() => pickFolder(f)}>☑</button>
          <button title="Rename folder" onClick={() => renameFolder(f)}>✎</button>
          <button title="New sub-folder" onClick={() => newFolder(f.id)}>＋</button>
          <button title="Delete folder" onClick={() => delFolder(f)}>✕</button>
        </span>
      </div>
      {expanded.has(f.id) && (
        <>
          {f.children.map((c) => renderFolder(c, depth + 1))}
          {f.use_cases.map((u) => {
            const e = eff(u);
            return (
              <button key={u.id} data-ucid={u.id}
                      className={"uc-leaf" + (selected?.id === u.id ? " sel" : "") + (u.in_scope ? "" : " oos")
                        + (picked.has(u.id) ? " picked" : "") + (activeCycle && !e.member ? " notin" : "")}
                      style={{ paddingLeft: 22 + depth * 14 }} onClick={(ev) => onLeafClick(ev, u)}
                      title="Click to open · Ctrl/⌘-click or Shift-click to select several">
                {picked.has(u.id) && <span className="uc-pick">✓</span>}
                {u.case_type === "functional"
                  ? <span className="uc-dot ct-functional" title="Functional case" />
                  : <span className={"uc-tag ct-" + u.case_type}>{CTYPE_LABEL[u.case_type]}</span>}
                {e.status !== "not_tested" && (
                  <span className={"uc-sdot st-" + e.status} title={"Test: " + TEST_STATUS_LABEL[e.status]} />
                )}
                {e.retest && <span className="uc-retest sm" title="Needs retest — the test definition changed since the verdict">⚠</span>}
                {!u.in_scope && <span className="uc-oosmark" title="Out of scope">⊘</span>}
                <span className="uc-id">{u.uc_id}</span> {u.title}
              </button>
            );
          })}
        </>
      )}
    </div>
  );

  const selEff = selected ? eff(selected) : null;
  const selPos = selected ? visibleCards.findIndex((u) => u.id === selected.id) : -1;
  const stepCard = (d: number) => { const u = visibleCards[selPos + d]; if (u) focusCard(u); };
  const allStepsOk = !!selected && selected.steps.length > 0 && selected.steps.every((_, i) => stepMarks[i] === "ok");
  const citations = selected && (
    <div className="uc-field">
      <div className="uc-flabel">Source requirements <em>(traceability — click to view)</em></div>
      <div className="uc-cites">
        {selected.source_chunk_ids.length === 0 && <span className="settings-hint">—</span>}
        {selected.source_chunk_ids.map((cid) => {
          const r = reqMap[cid];
          return (
            <button key={cid} className={"uc-cite" + (evCid === cid ? " open" : "") + (r?.archived ? " archived" : "")} dir="auto"
                    title={r ? citeSnippet(r.text, 240) : "Source requirement"}
                    onClick={() => setEvCid((c) => (c === cid ? null : cid))}>
              {titles[cid] || (r ? reqLabel(r) : "Requirement")}
              {r?.archived && <span className="uc-cite-archived">removed from the BRD</span>}
            </button>
          );
        })}
      </div>
      {evCid != null && reqMap[evCid] && (
        <>
          {reqMap[evCid].archived && (
            <div className="uc-ev-archived">
              This requirement was later removed from the BRD{reqMap[evCid].retired_at
                ? ` (on ${new Date(reqMap[evCid].retired_at as string).toLocaleDateString()})` : ""} — kept here for traceability.
            </div>
          )}
          <RequirementEvidence req={reqMap[evCid]} title={titles[evCid]} />
        </>
      )}
    </div>
  );

  return (
    <div className="ucpage">
      <div className="uc-toolbar">
        <span className="uc-proj" dir="auto" title={projLabel}>{projLabel}</span>
        <span className="reqpill">{count} {count === 1 ? "case" : "cases"}</span>
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
        {count > 0 && !gen && project && (
          <VersionBar project={project} refreshKey={allCards} toast={toast} askPrompt={askPrompt} confirm={confirm}
                      onDraftChanged={async () => { await load(); setHistKey((k) => k + 1); }}
                      reqLabel={(cid) => titles[cid] || (reqMap[cid] ? reqLabel(reqMap[cid]) : `Requirement #${cid}`)} />
        )}
        {count > 0 && !gen && <span className="uc-tb-sep" aria-hidden />}
        {count > 0 && !gen && project && (
          <TestCycleBar project={project} cycles={cycles} active={activeCycle} stats={stats} folders={folders}
                        onSelect={(id) => { setActiveCycleId(id); setStatusFilter("all"); }}
                        onChanged={loadCycles} onReport={() => setReportOpen(true)}
                        toast={toast} confirm={confirm} askPrompt={askPrompt} />
        )}
        {count > 0 && !gen && (
          <div className="uc-stdrop">
            <button className={"uc-tbbtn" + (activeFilters ? " on" : "")} onClick={() => setFilterOpen((o) => !o)}
                    aria-expanded={filterOpen} title="Filter the list · run options">
              ⚲ Filter{activeFilters ? <span className="n">{activeFilters}</span> : null}
            </button>
            {filterOpen && (
              <>
                <div className="uc-stdrop-backdrop" onClick={() => setFilterOpen(false)} />
                <div className="uc-stdrop-menu uc-filtermenu">
                  {hasMixedTypes && (
                    <>
                      <div className="uc-cyc-sep">Case type</div>
                      <div className="uc-filter">
                        <button className={"uc-chip" + (typeFilter === "all" ? " on" : "")} onClick={() => setTypeFilter("all")}>
                          All <span className="n">{count}</span>
                        </button>
                        {CTYPES.map((t) => (
                          <button key={t} className={"uc-chip ct-" + t + (typeFilter === t ? " on" : "")}
                                  disabled={!typeCounts[t]} onClick={() => setTypeFilter(t)}>
                            <span className={"dot ct-" + t} />{CTYPE_LABEL[t]} <span className="n">{typeCounts[t]}</span>
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                  <div className="uc-cyc-sep">{activeCycle ? `Status in “${activeCycle.name}”` : "Test status"}</div>
                  <div className="uc-filter">
                    <button className={"uc-chip" + (statusFilter === "all" ? " on" : "")} onClick={() => setStatusFilter("all")}>All</button>
                    {TSTATUS_ORDER.filter((s) => statusCounts[s]).map((s) => (
                      <button key={s} className={"uc-chip st-" + s + (statusFilter === s ? " on" : "")}
                              onClick={() => setStatusFilter(s)}>
                        <span className={"dot st-" + s} />{TEST_STATUS_LABEL[s]} <span className="n">{statusCounts[s]}</span>
                      </button>
                    ))}
                    {retestCount > 0 && (
                      <button className={"uc-chip uc-chip-retest" + (statusFilter === "retest" ? " on" : "")}
                              onClick={() => setStatusFilter("retest")} title="Verdict is stale — the test definition changed since it was recorded">
                        ⚠ Needs retest <span className="n">{retestCount}</span>
                      </button>
                    )}
                    {oosCount > 0 && (
                      <button className={"uc-chip uc-chip-oos" + (statusFilter === "oos" ? " on" : "")}
                              onClick={() => setStatusFilter("oos")} title="Out of scope — excluded from coverage">
                        ⊘ Out of scope <span className="n">{oosCount}</span>
                      </button>
                    )}
                  </div>
                  <div className="uc-cyc-sep">Run options</div>
                  {activeCycle && (
                    <label className="uc-optrow"><input type="checkbox" checked={cycleOnly} onChange={(e) => setCycleOnly(e.target.checked)} />
                      Only cases planned in this cycle</label>
                  )}
                  <label className="uc-optrow"><input type="checkbox" checked={autoAdvance}
                         onChange={(e) => { setAutoAdvance(e.target.checked); lsSet("brd.ucAutoAdvance", e.target.checked ? "1" : "0"); }} />
                    Auto-advance to the next case not yet run after a verdict</label>
                  {activeFilters > 0 && (
                    <button className="linkbtn uc-clearf" onClick={() => { setTypeFilter("all"); setStatusFilter("all"); }}>Clear filters</button>
                  )}
                </div>
              </>
            )}
          </div>
        )}
        <div className="uc-toolbar-sp" />
        {/* attention items only — everything else lives in Manage */}
        {count > 0 && !gen && status?.resume?.stale && (() => {
          const r = status.resume!;
          const edits = (r.changed ?? 0) + (r.removed ?? 0);
          const parts = [r.changed ? `${r.changed} changed` : "", r.added ? `${r.added} not covered yet` : "",
                         r.removed ? `${r.removed} removed` : ""].filter(Boolean).join(" · ");
          return (
            <button className="uc-tbbtn attn" onClick={() => runGenerate("sync")}
                    title={`Requirements: ${parts}. Only the use cases that depend on them are generated or updated `
                      + "— in place, in the draft — for you to review and publish as a new version."}>
              {edits ? `↻ Update (${edits} requirement${edits === 1 ? "" : "s"} changed)`
                     : `▶ Continue (${r.added} requirement${r.added === 1 ? "" : "s"} left)`}
            </button>
          );
        })()}
        {count === 0 && !gen && !loading && (
          <>
            <div className="uc-seg" role="group" title="Full analysis also derives negative + edge test cases (uses more tokens).">
              <button className={coverage === "functional" ? "on" : ""} onClick={() => chooseCoverage("functional")}>Functional</button>
              <button className={coverage === "full" ? "on" : ""} onClick={() => chooseCoverage("full")}>Full analysis</button>
            </div>
            <button className="uc-tbbtn attn" onClick={() => runGenerate("resume")}>Generate use cases</button>
          </>
        )}
        {count > 0 && (
          <div className="uc-stdrop">
            <button className="uc-tbbtn" disabled={gen} onClick={() => setManageOpen((o) => !o)} aria-expanded={manageOpen}
                    title="Folders, trash and generation">
              ⚙ Manage ▾
            </button>
            {manageOpen && (
              <>
                <div className="uc-stdrop-backdrop" onClick={() => setManageOpen(false)} />
                <div className="uc-stdrop-menu uc-managemenu">
                  <button className="uc-cyc-item act" onClick={() => { setManageOpen(false); newFolder(null); }}>＋ New folder</button>
                  <button className="uc-cyc-item act" onClick={() => { setManageOpen(false); openTrash(); }}>
                    🗑 Trash <em>deleted &amp; superseded cases (restorable)</em>
                  </button>
                  <div className="uc-cyc-sep">Generation</div>
                  <div className="uc-manage-row">
                    <span>Depth</span>
                    <div className="uc-seg" role="group" title="Applies to the next Regenerate. Full analysis also derives negative + edge test cases.">
                      <button className={coverage === "functional" ? "on" : ""} onClick={() => chooseCoverage("functional")}>Functional</button>
                      <button className={coverage === "full" ? "on" : ""} onClick={() => chooseCoverage("full")}>Full analysis</button>
                    </div>
                  </div>
                  <button className="uc-cyc-item act danger"
                          onClick={async () => {
                            setManageOpen(false);
                            const tested = allCards.filter((u) => u.test_status !== "not_tested").length;
                            const ok = await confirm(
                              "Regenerate rebuilds all use cases for this BRD from scratch, so every case gets a new code and the next "
                                + "version shows them all as removed and re-added. To follow requirement changes, use Update instead — it "
                                + "changes only the affected cases, in place. Current cases (with edits) go to Trash and stay restorable"
                                + (tested ? `; the ${tested} with test results keep them in their published version.` : "."),
                              "Regenerate all",
                            );
                            if (ok) runGenerate("replace");
                          }}>
                    ↻ Regenerate all… <em>rebuild the whole tree</em>
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {warn && (
        <div className="uc-warn">
          ⚠ {warn}
          <button className="linkbtn" onClick={() => setWarn(null)}>Dismiss</button>
        </div>
      )}

      <div className="uc-body">
        {!focus && <div className="uc-tree" style={{ flexBasis: treeWidth, width: treeWidth }}>
          {picked.size > 0 && (
            <div className="uc-bulk">
              <div className="uc-bulk-head">
                <b>{picked.size} selected</b>
                <button className="linkbtn" onClick={() => setPicked(new Set(visibleCards.map((u) => u.id)))}>all {visibleCards.length}</button>
                <span className="uc-tsp" />
                <button className="linkbtn" onClick={() => setPicked(new Set())}>Clear (Esc)</button>
              </div>
              <div className="uc-bulk-row">
                {BULK_VERDICTS.map((s) => (
                  <button key={s} className={"uc-chip st-" + s} onClick={() => bulkMark(s)} title={`Mark all selected ${TEST_STATUS_LABEL[s]}`}>
                    <span className={"dot st-" + s} />{TEST_STATUS_LABEL[s]}
                  </button>
                ))}
              </div>
              <div className="uc-bulk-row">
                <button className="uc-ghostbtn" onClick={() => bulkScope(false)}>⊘ Out of scope</button>
                <button className="uc-ghostbtn" onClick={() => bulkScope(true)}>Return to scope</button>
                {activeCycle && <button className="uc-ghostbtn" onClick={() => bulkCycle(true)}>＋ Plan into cycle</button>}
                {activeCycle && <button className="uc-ghostbtn" onClick={() => bulkCycle(false)}>－ Remove from cycle</button>}
              </div>
            </div>
          )}
          {loading && <div className="settings-hint">Loading…</div>}
          {!loading && count === 0 && gen && <div className="settings-hint">Generating use cases from this BRD… they’ll appear here as each scope completes.</div>}
          {!loading && count === 0 && !gen && <div className="settings-hint">No use cases yet for this BRD.</div>}
          {!loading && count > 0 && visibleFolders.length === 0 && (
            <div className="settings-hint">
              {activeCycle && cycleOnly && stats.total === 0
                ? <>No cases are planned in “{activeCycle.name}” yet — record a result on any case, or select cases and “Plan into cycle”. <button className="linkbtn" onClick={() => setCycleOnly(false)}>Show all cases</button></>
                : "No cases match these filters."}
            </div>
          )}
          {visibleFolders.map((f) => renderFolder(f, 0))}
        </div>}

        {!focus && <div
          className="resizer uc-resizer"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize use-case list"
          title="Drag to resize"
          onMouseDown={onTreeResize}
        />}

        <div className={"uc-detail" + (focus ? " focus" : "")}>
          {!selected ? (
            <div className="uc-empty">
              <div className="uc-empty-h">{activeCycle ? <>Test cycle <b>{activeCycle.name}</b></> : <>All results</>}</div>
              <div className="uc-empty-sub">{stats.executed} of {stats.total} cases run{stats.needs_retest ? ` · ${stats.needs_retest} need a retest` : ""}</div>
              {stats.total > 0 && <RunProgress wide s={stats} />}
              <div className="uc-empty-kpis">
                {(["passed", "failed", "blocked", "in_progress", "not_tested"] as TestStatus[]).map((s) => (
                  <button key={s} className={"uc-empty-kpi st-" + s} onClick={() => setStatusFilter(s)} title={`Show ${TEST_STATUS_LABEL[s]} cases`}>
                    <b>{stats.by_status[s] || 0}</b><span>{TEST_STATUS_LABEL[s]}</span>
                  </button>
                ))}
              </div>
              <button className="uc-compose-send on uc-empty-go"
                      onClick={() => { const u = nextNotRun(null); if (u) { chooseTab("run"); focusCard(u); } else toast("No cases left to run here 🎉"); }}>
                ▶ Start with the next case not run
              </button>
              <div className="uc-empty-tips">
                <div><kbd className="uc-kbd">N</kbd> next case not run · <kbd className="uc-kbd">J</kbd> <kbd className="uc-kbd">K</kbd> next / previous</div>
                <div><kbd className="uc-kbd">P</kbd> pass · <kbd className="uc-kbd">F</kbd> fail · <kbd className="uc-kbd">B</kbd> blocked · <kbd className="uc-kbd">I</kbd> in progress</div>
                <div>Ctrl/⌘-click or Shift-click cases to act on several at once · paste a screenshot straight into a case</div>
              </div>
            </div>
          ) : editing ? (
            <>
              <div className="uc-editrow"><label className="uc-elabel">Title</label>
                <input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></div>
              <div className="uc-editrow"><label className="uc-elabel">Case type</label>
                <select className="settings-select" value={draft.case_type}
                        onChange={(e) => setDraft({ ...draft, case_type: e.target.value as CType })}>
                  <option value="functional">Functional (happy path)</option>
                  <option value="negative">Negative (invalid / error)</option>
                  <option value="edge">Edge (boundary)</option>
                </select></div>
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
                <div className="uc-dtitle"><span className="uc-id">{selected.uc_id}</span> {selected.title}
                  <span className={"uc-ctype ct-" + selected.case_type}>{CTYPE_LABEL[selected.case_type]}</span>
                  {selEff && <span className={"uc-tstatus sm st-" + selEff.status}>{TEST_STATUS_LABEL[selEff.status]}</span>}
                  {selEff?.retest && <span className="uc-retest" title="The test definition changed since the last verdict">⚠ needs retest</span>}
                  {!selected.in_scope && <span className="uc-oosmark lg" title="Out of scope — excluded from coverage">⊘ out of scope</span>}</div>
                <div className="uc-detacts">
                  <button className="linkbtn" onClick={startEdit}>Edit</button>
                  <button className="linkbtn danger" onClick={doDelete}>Delete</button>
                </div>
              </div>
              {selected.last_at && (
                <button className="uc-lastchg" onClick={() => chooseTab("audit")} title="Open the audit log">
                  <span className="uc-avatar sm" aria-hidden>{selected.last_by ? selected.last_by[0].toUpperCase() : "⚙"}</span>
                  <span>Last change:</span>
                  <span className="uc-lastchg-sum" dir="auto">{selected.last_summary || "Changed"}</span>
                  <span>by <b>{selected.last_by || "System"}</b></span>
                  <span title={new Date(selected.last_at).toLocaleString()}>· {ago(selected.last_at)}</span>
                </button>
              )}
              <div className="uc-tabs" role="tablist">
                {(["overview", "run", "audit"] as DetailTab[]).map((t) => (
                  <button key={t} role="tab" aria-selected={tab === t} className={"uc-tab" + (tab === t ? " on" : "")}
                          onClick={() => chooseTab(t)}>
                    {t === "run" ? "▶ Run" : t === "overview" ? "Overview" : "Audit log"}
                  </button>
                ))}
                <span className="uc-tsp" />
                {selPos >= 0 && (
                  <span className="uc-nav">
                    <button onClick={() => stepCard(-1)} disabled={selPos <= 0} title="Previous case (K)">‹</button>
                    <span>{selPos + 1} / {visibleCards.length}</span>
                    <button onClick={() => stepCard(1)} disabled={selPos >= visibleCards.length - 1} title="Next case (J)">›</button>
                    <button className="uc-nav-next" onClick={() => { const u = nextNotRun(selected.id); if (u) focusCard(u); else toast("No cases left to run here 🎉"); }}
                            title="Next case not yet run (N)">Next not run ⇥</button>
                  </span>
                )}
                <button className={"uc-focusbtn" + (focus ? " on" : "")} onClick={toggleFocus}
                        title={focus ? "Show the use-case list again" : "Hide the list and give the workspace the full width"}>
                  {focus ? "⤡ Exit focus" : "⤢ Focus"}
                </button>
              </div>

              {tab === "overview" && (
                <div className="uc-overview">
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
                  {citations}
                  <button className="uc-compose-send on uc-runcta" onClick={() => chooseTab("run")}>▶ Run this case</button>
                </div>
              )}

              {tab === "run" && (
                <div className="uc-run-grid">
                  <section className="uc-script">
                    <div className="uc-script-h">Test script</div>
                    {selected.roles.length > 0 && <div className="uc-script-roles">As <b>{selected.roles.join(", ")}</b></div>}
                    {selected.preconditions && (
                      <div className="uc-script-pre"><div className="uc-flabel">Preconditions</div><div dir="auto">{selected.preconditions}</div></div>
                    )}
                    <div className="uc-flabel">Steps <em>— tick each as you go; ✕ fails the case at that step</em></div>
                    <ol className="uc-checklist">
                      {selected.steps.map((s, i) => (
                        <li key={i} className={stepMarks[i] ? "m-" + stepMarks[i] : ""}>
                          <span className="uc-stepn">{i + 1}</span>
                          <span className="uc-stept" dir="auto">{s}</span>
                          <span className="uc-stepacts">
                            <button className={"ok" + (stepMarks[i] === "ok" ? " on" : "")} title="Step behaves as expected"
                                    onClick={() => setStepMarks((m) => { const n = { ...m }; if (n[i] === "ok") delete n[i]; else n[i] = "ok"; return n; })}>✓</button>
                            <button className={"fail" + (stepMarks[i] === "fail" ? " on" : "")} title="Fail the case at this step"
                                    onClick={() => {
                                      setStepMarks((m) => ({ ...m, [i]: "fail" }));
                                      setRunReq({ status: "failed", note: `Step ${i + 1} failed: ${s}\nActual: `, nonce: Date.now() });
                                    }}>✕</button>
                          </span>
                        </li>
                      ))}
                      {selected.steps.length === 0 && <li className="none">No steps defined.</li>}
                    </ol>
                    {allStepsOk && (
                      <button className="uc-allok" onClick={() => setRunReq({ status: "passed", note: `All ${selected.steps.length} steps passed.`, nonce: Date.now() })}>
                        ✓ All steps passed — record Passed
                      </button>
                    )}
                    <div className="uc-script-exp"><div className="uc-flabel">Expected result</div><div dir="auto">{selected.expected_behaviour || "—"}</div></div>
                    {citations}
                  </section>
                  <section className="uc-run-exec">
                    <UseCaseTesting project={project || ""} ucId={selected.id} uid={selected.uid} cycle={activeCycle}
                                    refreshKey={histKey} keyboard={!modalOpen && !editing} request={runReq}
                                    onCard={onCard} toast={toast} confirm={confirm} />
                  </section>
                </div>
              )}

              {tab === "audit" && (
                <UseCaseAudit uid={selected.uid} ucLabel={selected.uc_id || selected.title} refreshKey={auditKey}
                              onRestored={async () => { await load(); setHistKey((k) => k + 1); }}
                              toast={toast} confirm={confirm} />
              )}
            </>
          )}
        </div>
      </div>

      {reportOpen && project && (
        <TestReport project={project} projLabel={projLabel} cycles={cycles} initialCycleId={activeCycleId}
                    onClose={() => setReportOpen(false)}
                    onOpenCase={(id) => { const u = findUc(folders, id); setReportOpen(false); if (u) focusCard(u); }} />
      )}

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
