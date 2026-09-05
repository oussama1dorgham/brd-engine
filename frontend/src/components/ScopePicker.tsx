import { useEffect, useMemo, useRef, useState } from "react";
import type { Project } from "../types";
import { etaText } from "../lib/format";

export default function ScopePicker({
  projects, activeProject, projTitle, scopeLocked, onChoose,
}: {
  projects: Project[];
  activeProject: string | null;
  projTitle: Record<string, string>;
  scopeLocked: boolean;
  onChoose: (project: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const wrapRef = useRef<HTMLDivElement>(null);

  const label = activeProject ? projTitle[activeProject] || activeProject : null;
  const ready = useMemo(() => projects.filter((p) => (p.status ?? "ready") === "ready"), [projects]);
  const proc = projects.find((p) => p.status === "processing");
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    return ready.filter((p) => (p.title || "").toLowerCase().includes(s) || (p.project || "").toLowerCase().includes(s));
  }, [q, ready]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  // Locked conversation → static label, no dropdown.
  if (scopeLocked) {
    return (
      <span className="scopepill" title="This conversation is locked to this BRD">
        {label || "No BRD selected"}
      </span>
    );
  }

  const choose = (p: Project) => { onChoose(p.project); setOpen(false); setQ(""); };

  return (
    <div className="scopewrap" ref={wrapRef}>
      <button
        className={"scopepill clickable" + (activeProject ? "" : " unset")}
        title="Choose a BRD to ground this chat — your first message locks it"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {label || "Select a BRD"}
        <span className="scopecaret" aria-hidden="true" />
      </button>
      {open && (
        <div className="scopemenu">
          {ready.length > 4 && (
            <input className="scopesearch" autoFocus dir="auto" placeholder="Search BRDs…" value={q} onChange={(e) => setQ(e.target.value)} />
          )}
          <div className="scopelist">
            {ready.length === 0 ? (
              <div className="brdopt-empty">
                {proc
                  ? `A BRD is processing${typeof proc.progress === "number" ? ` — ${proc.progress}%` : "…"}${etaText(proc.eta_seconds) ? " · " + etaText(proc.eta_seconds) : ""}`
                  : "No BRDs yet — add one via Manage BRDs"}
              </div>
            ) : filtered.length === 0 ? (
              <div className="brdopt-empty">No matching BRDs</div>
            ) : (
              filtered.map((p) => (
                <div key={p.project} className={"brdopt" + (p.project === activeProject ? " active" : "")} dir="auto"
                  onMouseDown={(e) => { e.preventDefault(); choose(p); }}>
                  <div className="ot">{p.title || p.project}</div>
                  <div className="sub">{p.project} · {p.docs} doc{p.docs === 1 ? "" : "s"}</div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
