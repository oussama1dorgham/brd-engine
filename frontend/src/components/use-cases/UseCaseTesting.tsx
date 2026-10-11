import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteProof, editTestEvent, getUseCaseTimeline, getVersionCase, needsReason, proofUrl, recordTest, setScope, stageProofLink,
  undoTest, uploadProofFile, TEST_STATUSES, TEST_STATUS_KEY, TEST_STATUS_LABEL,
  type TestCycle, type TestStatus, type UcCardState, type UcTestEvent, type UcTestProof, type UcWrite,
  type VersionCase,
} from "../../lib/api";
import ProofLightbox from "./ProofLightbox";

const isUrl = (s: string) => /^https?:\/\/\S+$/i.test(s.trim());
const fmtSize = (n: number | null) =>
  n == null ? "" : n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
// a keypress belongs to the page (not a text field / menu the user is typing in)
export const isTypingTarget = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
const MAX_PARALLEL_UPLOADS = 3;

function DefectRef({ value }: { value: string }) {
  return isUrl(value)
    ? <a className="uc-defect" href={value} target="_blank" rel="noreferrer" title={value}>🐞 {value.replace(/^https?:\/\/(www\.)?/, "")}</a>
    : <span className="uc-defect" title="Defect reference">🐞 {value}</span>;
}

function ProofChip({ p, removable, onDelete, onOpen }: {
  p: UcTestProof; removable: boolean; onDelete: () => void; onOpen?: () => void;
}) {
  const href = p.kind === "link" ? (p.url || "#") : proofUrl(p.id);
  const label = p.caption || p.filename || p.url || "proof";
  return (
    <span className={"uc-proof k-" + p.kind + (p.result_id == null ? " staged" : "")} title={label}>
      {p.kind === "image" ? (
        <button className="uc-proof-thumb" onClick={onOpen} title={`${label} — click to enlarge`}>
          <img src={href} alt={label} loading="lazy" />
        </button>
      ) : p.kind === "video" && onOpen ? (
        <button className="uc-proof-link" onClick={onOpen}>🎬 {label}{p.size != null && <em> · {fmtSize(p.size)}</em>}</button>
      ) : (
        <a href={href} target="_blank" rel="noreferrer noopener" className="uc-proof-link">
          {p.kind === "link" ? "🔗" : "📄"} {label}
          {p.size != null && <em> · {fmtSize(p.size)}</em>}
        </a>
      )}
      {removable && <button className="uc-proof-x" title="Remove evidence" onClick={onDelete}>✕</button>}
    </span>
  );
}

type Upload = { key: string; name: string; progress: number };

// Per-use-case TEST EXECUTION — one dynamic flow:
//   capture evidence (drop / paste / 📎 / link) → it is STAGED → pick a verdict (menu or P/F/B/I;
//   Failed/Blocked ask for a reason + defect) → the verdict claims the staged evidence and is
//   recorded into the active test cycle. A quick re-verdict amends; the latest entry is undoable.
// Writes return the event + card state, so nothing refetches. See docs/USECASE_TESTING.md.
// A cycle tests one published version. When the draft moved on after it, the tester must follow
// the VERSION's definition — shown here on demand (steps, expected result, cited requirements).
function VersionDefinition({ project, cycle, uid, inVersion }: {
  project: string; cycle: TestCycle; uid: number; inVersion: boolean;
}) {
  const [def, setDef] = useState<VersionCase | null>(null);
  const [open, setOpen] = useState(false);
  const v = cycle.version!;
  const show = async () => {
    if (!open && !def) {
      const d = await getVersionCase(project, v.id, uid).catch(() => null);
      if (d && !d.error) setDef(d);
    }
    setOpen((o) => !o);
  };
  if (!inVersion) {
    return (
      <div className="uc-warn uc-verdef">
        This case was added after v{v.number}, so this cycle (which tests v{v.number}) can't record it. Publish a new
        version and start a new cycle to test it.
      </div>
    );
  }
  const steps = Array.isArray(def?.case.steps) ? (def!.case.steps as string[]) : [];
  return (
    <div className="uc-warn uc-verdef">
      <div>This case changed in the draft after v{v.number}. This cycle tests <b>v{v.number}'s definition</b> — follow it,
        not the current draft. <button className="linkbtn" onClick={show}>{open ? "Hide" : `Show v${v.number} definition`}</button></div>
      {open && def && (
        <div className="uc-verdef-body" dir="auto">
          {def.case.preconditions && <><div className="uc-flabel">Preconditions</div><div>{def.case.preconditions}</div></>}
          <div className="uc-flabel">Steps</div>
          <ol>{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
          <div className="uc-flabel">Expected behaviour</div><div>{def.case.expected_behaviour}</div>
          {def.requirements.map((r) => (
            <div key={r.chunk_id} className="uc-verdef-req"><span className="uc-flabel">Requirement as of v{def.version}</span>{r.text}</div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function UseCaseTesting({ project, ucId, uid, cycle, refreshKey, keyboard, request, onCard, toast, confirm }: {
  project: string;
  ucId: number;
  uid: number;
  cycle: TestCycle | null;           // active test cycle (verdicts are recorded into it) — null = ad-hoc
  refreshKey?: number;               // bumped when the definition changes (staleness may move)
  keyboard: boolean;                 // P/F/B/I shortcuts enabled (page decides: no modal open, etc.)
  request?: { status: TestStatus; note?: string; nonce: number } | null;   // verdict asked for by the step checklist
  onCard: (card: UcCardState, kind: "status" | "note" | "scope" | "undo") => void;
  toast: (m: string) => void;
  confirm: (msg: string, yesLabel: string) => Promise<boolean>;
}) {
  const [events, setEvents] = useState<UcTestEvent[]>([]);
  const [pending, setPending] = useState<UcTestProof[]>([]);
  const [card, setCard] = useState<UcCardState | null>(null);
  const [windowSec, setWindowSec] = useState(300);
  const [loading, setLoading] = useState(false);
  const [text, setText] = useState("");                         // one field: a note, or a pasted link
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [reason, setReason] = useState<TestStatus | null>(null);   // Failed/Blocked reason panel
  const [reasonNote, setReasonNote] = useState("");
  const [defect, setDefect] = useState("");
  const [editing, setEditing] = useState<number | null>(null);      // latest event being corrected
  const [editNote, setEditNote] = useState("");
  const [editDefect, setEditDefect] = useState("");
  const [undoUntil, setUndoUntil] = useState<Record<number, number>>({});
  const [, setTick] = useState(0);
  const [lightbox, setLightbox] = useState<{ items: UcTestProof[]; index: number } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const reasonRef = useRef<HTMLTextAreaElement | null>(null);
  const cycleId = cycle?.id ?? null;
  const cycleClosed = cycle?.status === "closed";

  const load = useCallback(async () => {
    setLoading(true);
    const t = await getUseCaseTimeline(uid, cycleId).catch(() => null);
    setLoading(false);
    if (!t) return;
    setEvents(t.events); setPending(t.pending); setCard(t.card); setWindowSec(t.amend_window_seconds);
    // resume the undo countdown of a still-undoable latest entry
    const now = Date.now();
    const u: Record<number, number> = {};
    for (const e of t.events) {
      if (!e.undoable) continue;
      const since = now - Date.parse(e.amended_at || e.tested_at);
      u[e.result_id] = now + Math.max(5_000, t.amend_window_seconds * 1000 - Math.max(0, since));
    }
    setUndoUntil(u);
  }, [uid, cycleId]);

  useEffect(() => { setText(""); setReason(null); setEditing(null); setMenuOpen(false); load(); }, [uid, load]);
  useEffect(() => { if (refreshKey) load(); }, [refreshKey, load]);
  // re-render when an undo countdown lapses so the button disappears on time
  useEffect(() => {
    const next = Math.min(...Object.values(undoUntil).filter((t) => t > Date.now()));
    if (!Number.isFinite(next)) return;
    const h = window.setTimeout(() => setTick((n) => n + 1), next - Date.now() + 50);
    return () => window.clearTimeout(h);
  }, [undoUntil]);

  // fold a write's {event, card} into local state — no refetch
  const applyWrite = (r: UcWrite, kind: "status" | "note" | "scope") => {
    const ev = r.event;
    if (ev) {
      setEvents((es) => (r.amended
        ? es.map((e) => (e.result_id === ev.result_id ? ev : e))
        : [ev, ...es.map((e) => ({ ...e, latest: false, undoable: false }))]));
      setUndoUntil({ [ev.result_id]: Date.now() + windowSec * 1000 });
    }
    if (r.claimed) setPending([]);
    if (r.card) { setCard(r.card); onCard(r.card, kind); }
  };

  const record = async (s: TestStatus, note?: string, defectRef?: string) => {
    setBusy(true);
    const r = await recordTest(ucId, { status: s, note, defect_ref: defectRef, cycle_id: cycleId });
    setBusy(false);
    if (r.error) { toast(r.error); return false; }
    applyWrite(r, "status");
    toast(r.amended ? `Changed to ${TEST_STATUS_LABEL[s]}` : `Marked ${TEST_STATUS_LABEL[s]} ✓`);
    return true;
  };

  // Pick a verdict: Failed/Blocked open a reason + defect prompt (optional, Enter records);
  // the others record at once. A plain note typed in the composer rides along.
  const choose = async (s: TestStatus) => {
    setMenuOpen(false);
    if (cycleClosed) { toast("This test cycle is closed — reopen it to record results"); return; }
    const typed = text.trim() && !isUrl(text) ? text.trim() : "";
    if (needsReason(s)) {
      setReason(s); setReasonNote(typed); setDefect("");
      if (typed) setText("");        // the typed note moves into the reason; the field below takes evidence
      window.setTimeout(() => reasonRef.current?.focus(), 0);
      return;
    }
    if (await record(s, typed || undefined) && typed) setText("");
  };

  // A verdict requested from outside (the script's step checklist): a failed step opens the
  // reason prompt pre-filled with that step; "all steps passed" records Passed.
  useEffect(() => {
    if (!request) return;
    if (cycleClosed) { toast("This test cycle is closed — reopen it to record results"); return; }
    if (needsReason(request.status)) {
      setReason(request.status); setReasonNote(request.note || ""); setDefect("");
      window.setTimeout(() => reasonRef.current?.focus(), 0);
    } else {
      record(request.status, request.note);
    }
  }, [request?.nonce]);  // eslint-disable-line react-hooks/exhaustive-deps

  const submitReason = async () => {
    if (!reason) return;
    const ok = await record(reason, reasonNote.trim() || undefined, defect.trim() || undefined);
    if (!ok) return;
    if (reasonNote.trim() && reasonNote.trim() === text.trim()) setText("");
    setReason(null);
  };

  // The composer's primary action adapts: a URL is staged as a link proof; text is logged as
  // a note; with only staged evidence, it logs the evidence on its own.
  const send = async () => {
    const t = text.trim();
    if (t && isUrl(t)) {
      setBusy(true);
      const r = await stageProofLink(ucId, t);
      setBusy(false);
      if (r.error || !r.proof) { toast(r.error || "Could not attach link"); return; }
      setPending((p) => [...p, r.proof!]); setText("");
      return;
    }
    if (!t && pending.length === 0) return;
    setBusy(true);
    const r = await recordTest(ucId, { event_kind: "note", note: t || undefined, cycle_id: cycleId });
    setBusy(false);
    if (r.error) { toast(r.error); return; }
    setText("");
    applyWrite(r, "note");
    toast(t ? "Note added ✓" : "Evidence logged ✓");
  };

  const sendFiles = async (files: FileList | File[]) => {
    const arr = Array.from(files);
    if (!arr.length) return;
    if (fileRef.current) fileRef.current.value = "";
    const queue = arr.map((f, i) => ({ f, key: `${Date.now()}-${i}-${f.name}` }));
    setUploads((u) => [...u, ...queue.map((q) => ({ key: q.key, name: q.f.name, progress: 0 }))]);
    let ok = 0;
    const worker = async () => {
      for (let q = queue.shift(); q; q = queue.shift()) {
        const { f, key } = q;
        const r = await uploadProofFile(ucId, f, (fr) =>
          setUploads((u) => u.map((x) => (x.key === key ? { ...x, progress: fr } : x))));
        setUploads((u) => u.filter((x) => x.key !== key));
        if (r.error || !r.proof) toast(`${f.name}: ${r.error || "upload failed"}`);
        else { ok++; setPending((p) => [...p, r.proof!]); }
      }
    };
    await Promise.all(Array.from({ length: Math.min(MAX_PARALLEL_UPLOADS, arr.length) }, worker));
    if (ok) toast(`${ok} proof${ok > 1 ? "s" : ""} staged — they attach to your next verdict or note`);
  };

  const onPaste = async (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files);
    if (files.length) { e.preventDefault(); await sendFiles(files); }
  };

  const removeProof = async (p: UcTestProof) => {
    if (p.result_id != null && !(await confirm("Remove this evidence from the recorded result?", "Remove"))) return;
    const r = await deleteProof(p.id);
    if (r.error) { toast(r.error); return; }
    if (p.result_id == null) setPending((ps) => ps.filter((x) => x.id !== p.id));
    else setEvents((es) => es.map((e) => (e.result_id === p.result_id
      ? { ...e, proofs: e.proofs.filter((x) => x.id !== p.id) } : e)));
  };

  const undo = async (ev: UcTestEvent) => {
    const r = await undoTest(ev.result_id, cycleId);
    if (r.error) { toast(r.error); await load(); return; }
    if (r.restored_note && !text.trim()) setText(r.restored_note);
    await load();                                   // proofs may have returned to staging
    if (r.card) { setCard(r.card); onCard(r.card, "undo"); }
    toast(r.reverted ? "Reverted ✓" : "Undone ✓");
  };

  const saveEdit = async (ev: UcTestEvent) => {
    const r = await editTestEvent(ev.result_id, { note: editNote, defect_ref: editDefect });
    if (r.error || !r.event) { toast(r.error || "Could not save"); return; }
    setEvents((es) => es.map((e) => (e.result_id === ev.result_id ? r.event! : e)));
    setEditing(null);
  };

  const toggleScope = async () => {
    const inScope = card?.in_scope ?? true;
    if (inScope && !(await confirm(
      "Mark this test out of scope? It's excluded from coverage until you bring it back — its verdicts and evidence are kept.",
      "Mark out of scope",
    ))) return;
    setBusy(true);
    const r = await setScope(ucId, !inScope, undefined, cycleId);
    setBusy(false);
    if (r.error) { toast(r.error); return; }
    applyWrite(r, "scope");
    toast(inScope ? "Marked out of scope" : "Back in scope ✓");
  };

  // keyboard verdicts: P / F / B / I (the page enables this only when nothing else has focus)
  useEffect(() => {
    if (!keyboard) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || isTypingTarget(e.target) || lightbox || reason || busy) return;
      const s = (Object.keys(TEST_STATUS_KEY) as TestStatus[]).find((k) => TEST_STATUS_KEY[k] === e.key.toUpperCase());
      if (s) { e.preventDefault(); choose(s); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });   // re-bound every render so the handler sees current state

  const openLightbox = (proofs: UcTestProof[], p: UcTestProof) => {
    const items = proofs.filter((x) => x.kind === "image" || x.kind === "video");
    setLightbox({ items, index: Math.max(0, items.findIndex((x) => x.id === p.id)) });
  };

  const overall = card?.test_status ?? "not_tested";
  const cur: TestStatus = cycle ? (card?.cycle?.status ?? "not_tested") : overall;
  const needsRetest = cycle ? !!card?.cycle?.needs_retest : !!card?.needs_retest;
  const inScope = card?.in_scope ?? true;
  const urlMode = isUrl(text);
  const now = Date.now();
  const sendLabel = urlMode ? "Attach link" : text.trim() ? "Log note" : pending.length ? "Log evidence" : "Log note";

  return (
    <div className={"uc-test" + (inScope ? "" : " oos")} onPaste={onPaste}>
      <div className="uc-test-title">
        <span>Execution</span>
        <span className={"uc-tctx-chip" + (cycle ? (cycleClosed ? " closed" : " on") : "")}
              title={cycle ? (cycleClosed ? "This cycle is closed — results are read-only" : "Verdicts are recorded into this test cycle")
                : "Not part of a test cycle — pick one in the toolbar to track this run"}>
          {cycle ? (cycleClosed ? "🔒 " : "● ") + cycle.name : "Ad-hoc run"}
        </span>
        {cycle && overall !== cur && (
          <span className="uc-tctx-overall">overall latest <span className={"uc-tstatus sm st-" + overall}>{TEST_STATUS_LABEL[overall]}</span></span>
        )}
      </div>

      <div className="uc-test-head">
        <span className="uc-flabel">Status</span>
        <div className="uc-stdrop">
          <button className="uc-stdrop-btn" disabled={busy || cycleClosed} aria-haspopup="listbox" aria-expanded={menuOpen}
                  onClick={() => setMenuOpen((o) => !o)}>
            <span className={"uc-tstatus st-" + cur}>{TEST_STATUS_LABEL[cur]}</span>
            <span className="uc-caret">▾</span>
          </button>
          {menuOpen && (
            <>
              <div className="uc-stdrop-backdrop" onClick={() => setMenuOpen(false)} />
              <div className="uc-stdrop-menu" role="listbox">
                {TEST_STATUSES.map((s) => (
                  <button key={s} role="option" aria-selected={cur === s}
                          className="uc-stdrop-item" disabled={busy} onClick={() => choose(s)}
                          title={`Record ${TEST_STATUS_LABEL[s]}`}>
                    <span className={"uc-tstatus st-" + s}>{TEST_STATUS_LABEL[s]}</span>
                    {cur === s ? <span className="uc-stdrop-check">✓</span>
                      : TEST_STATUS_KEY[s] && <kbd className="uc-kbd">{TEST_STATUS_KEY[s]}</kbd>}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
        {needsRetest && <span className="uc-retest" title="The test definition changed since this verdict — re-run and record a fresh result">⚠ needs retest</span>}
        {cycle && card?.cycle && !card.cycle.in_cycle && <span className="uc-tnotin" title="Not planned in this cycle — recording a result adds it">not in cycle</span>}
        {!inScope && <span className="uc-oosmark lg" title="Excluded from coverage">⊘ out of scope</span>}
        <span className="uc-tsp" />
        <button className="uc-ghostbtn" disabled={busy} onClick={toggleScope}>
          {inScope ? "Mark out of scope" : "Return to scope"}
        </button>
      </div>
      {cycle?.version && card?.cycle && (card.cycle.draft_differs || card.cycle.in_version === false) && (
        <VersionDefinition key={`${cycle.id}-${uid}`} project={project} cycle={cycle} uid={uid}
                           inVersion={card.cycle.in_version !== false} />
      )}

      {/* One capture area. Normally: the adaptive composer (note / link / files). With Failed or
          Blocked chosen it becomes a single form read top-to-bottom — reason → defect → evidence
          (optional, but placed so it feels like the natural next step) → Record. */}
      <div className={reason ? "uc-reason st-" + reason : "uc-capture"}>
        {reason && (
          <>
            <div className="uc-reason-head">
              Record <span className={"uc-tstatus sm st-" + reason}>{TEST_STATUS_LABEL[reason]}</span>
              <span className="settings-hint">{reason === "failed" ? "What went wrong?" : "What's blocking it?"}</span>
            </div>
            <textarea ref={reasonRef} rows={2} value={reasonNote} placeholder={reason === "failed" ? "Actual vs expected…" : "Blocked by…"}
                      onChange={(e) => setReasonNote(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submitReason(); }
                        if (e.key === "Escape") setReason(null);
                      }} />
            <input value={defect} placeholder="Defect link or id (e.g. https://jira/…/BUG-123)"
                   onChange={(e) => setDefect(e.target.value)}
                   onKeyDown={(e) => {
                     if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitReason();
                     if (e.key === "Escape") setReason(null);
                   }} />
            <div className="uc-flabel uc-reason-evh">Evidence <em>— optional: screenshots, logs, video or links</em></div>
          </>
        )}
        <div className={"uc-compose" + (drag ? " over" : "")}
             onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
             onDragLeave={() => setDrag(false)}
             onDrop={(e) => { e.preventDefault(); setDrag(false); sendFiles(e.dataTransfer.files); }}>
          <button className="uc-compose-clip" title="Attach files" disabled={busy} onClick={() => fileRef.current?.click()}>📎</button>
          <input className="uc-compose-in" value={text}
                 placeholder={reason ? "Paste a link or a screenshot, or drop / 📎 files…" : "Add a note, paste a link or a screenshot…"}
                 onChange={(e) => setText(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && (!reason || urlMode)) send(); }} />
          <button className="uc-compose-send"
                  disabled={busy || (reason ? !urlMode : !text.trim() && pending.length === 0)} onClick={send}>
            {reason ? "Attach link" : sendLabel}
          </button>
          <input ref={fileRef} type="file" multiple hidden accept="image/*,video/*,.txt,.log,.json,.pdf,.har,.zip,.csv,.xml"
                 onChange={(e) => e.target.files && sendFiles(e.target.files)} />
        </div>
        {(pending.length > 0 || uploads.length > 0) && (
          <div className="uc-staged">
            <span className="uc-staged-label">{reason ? "Attached" : "Staged"}</span>
            {pending.map((p) => (
              <ProofChip key={p.id} p={p} removable onDelete={() => removeProof(p)} onOpen={() => openLightbox(pending, p)} />
            ))}
            {uploads.map((u) => (
              <span key={u.key} className="uc-upload" title={u.name}>
                <span className="uc-upload-name">{u.name}</span>
                <span className="uc-bar"><span className="uc-bar-fill" style={{ width: `${Math.round(u.progress * 100)}%` }} /></span>
              </span>
            ))}
          </div>
        )}
        {reason ? (
          <div className="uc-reason-acts">
            <span className={"uc-reason-evstate" + (pending.length ? " has" : "")}>
              {uploads.length ? `Uploading ${uploads.length} file${uploads.length > 1 ? "s" : ""}…`
                : pending.length ? `✓ ${pending.length} piece${pending.length > 1 ? "s" : ""} of evidence will attach`
                : "No evidence yet — optional, you can record without it"}
            </span>
            <span className="uc-tsp" />
            <button className="backbtn" onClick={() => setReason(null)}>Cancel</button>
            <button className="uc-compose-send on" disabled={busy || uploads.length > 0} onClick={submitReason}
                    title="Record (Ctrl/⌘ + Enter)">
              Record {TEST_STATUS_LABEL[reason]}
            </button>
          </div>
        ) : (
          <div className="uc-compose-hint">Drop, paste or 📎 evidence — it's staged and attaches to your next verdict.</div>
        )}
      </div>

      {/* activity timeline */}
      <div className="uc-tline-h">Activity</div>
      <div className="uc-tline">
        {loading && events.length === 0 && <div className="settings-hint">Loading…</div>}
        {!loading && events.length === 0 && (
          <div className="settings-hint">No test activity yet — set a verdict or attach evidence above.</div>
        )}
        {events.map((ev) => {
          const canUndo = ev.latest && (undoUntil[ev.result_id] ?? 0) > now;
          const inThisCycle = cycle && ev.cycle_id === cycle.id;
          return (
            <div key={ev.result_id} className={"uc-tevent" + (inThisCycle ? " incycle" : "") + (ev.stale ? " stale" : "")}>
              <div className="uc-teline">
                {ev.event_kind === "note" ? (
                  <span className="uc-tkind note">Note</span>
                ) : ev.event_kind === "scope" ? (
                  <span className="uc-tkind scope">{ev.in_scope ? "Back in scope" : "Out of scope"}</span>
                ) : (
                  <span className={"uc-tstatus sm st-" + ev.status}>{TEST_STATUS_LABEL[ev.status]}</span>
                )}
                {ev.amended_from && <span className="uc-tamend" title="Corrected within the amend window">was {TEST_STATUS_LABEL[ev.amended_from]}</span>}
                {ev.carried_from && <span className="uc-tcarried" title="Carried into this cycle unchanged — the original result holds the evidence">carried</span>}
                {ev.cycle_name && <span className="uc-tcycle" title="Test cycle">{ev.cycle_name}</span>}
                <span className="uc-twhen">{new Date(ev.tested_at).toLocaleString()}</span>
                {ev.tested_by && <span className="uc-twho">{ev.tested_by}</span>}
                {ev.event_kind === "status" && ev.tested_against_ver != null && (
                  <span className="uc-tver" title="Definition version this was tested against">def v{ev.tested_against_ver}</span>
                )}
                {ev.stale && <span className="uc-retest sm" title="Recorded against an older test definition">⚠ outdated</span>}
                <span className="uc-tsp" />
                {ev.latest && editing !== ev.result_id && ev.event_kind !== "scope" && (
                  <button className="uc-tact" onClick={() => { setEditing(ev.result_id); setEditNote(ev.note || ""); setEditDefect(ev.defect_ref || ""); }}>Edit</button>
                )}
                {canUndo && <button className="uc-tact undo" onClick={() => undo(ev)}>↶ Undo</button>}
              </div>
              {editing === ev.result_id ? (
                <div className="uc-tedit">
                  <textarea rows={2} value={editNote} placeholder="Note" onChange={(e) => setEditNote(e.target.value)} />
                  {(ev.event_kind === "note" || needsReason(ev.status)) && (
                    <input value={editDefect} placeholder="Defect link or id" onChange={(e) => setEditDefect(e.target.value)} />
                  )}
                  <div className="uc-reason-acts">
                    <span className="uc-tsp" />
                    <button className="backbtn" onClick={() => setEditing(null)}>Cancel</button>
                    <button className="uc-compose-send on" onClick={() => saveEdit(ev)}>Save</button>
                  </div>
                </div>
              ) : (
                <>
                  {ev.note && <div className="uc-tnotetext">{ev.note}</div>}
                  {ev.defect_ref && <div className="uc-tdefect"><DefectRef value={ev.defect_ref} /></div>}
                </>
              )}
              {ev.proofs.length > 0 && (
                <div className="uc-proofs">
                  {ev.proofs.map((p) => (
                    <ProofChip key={p.id} p={p} removable={ev.latest} onDelete={() => removeProof(p)}
                               onOpen={() => openLightbox(ev.proofs, p)} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {lightbox && <ProofLightbox items={lightbox.items} index={lightbox.index} onClose={() => setLightbox(null)} />}
    </div>
  );
}
