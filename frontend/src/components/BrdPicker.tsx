import { useMemo, useState } from "react";
import type { Project } from "../types";
import { etaText } from "../lib/format";

export default function BrdPicker({
  projects,
  activeProject,
  projTitle,
  onChoose,
}: {
  projects: Project[];
  activeProject: string | null;
  projTitle: Record<string, string>;
  onChoose: (project: string) => void;
}) {
  const selectedTitle = activeProject ? projTitle[activeProject] || activeProject : "";
  const [q, setQ] = useState(selectedTitle);
  const [open, setOpen] = useState(false);
  const [idx, setIdx] = useState(-1);

  const ready = useMemo(() => projects.filter((p) => (p.status ?? "ready") === "ready"), [projects]);

  const filtered = useMemo(() => {
    // When the box still shows the current selection, list everything for easy browsing.
    const showAll = !!activeProject && q === selectedTitle;
    const s = (showAll ? "" : q).trim().toLowerCase();
    return ready.filter(
      (p) => (p.title || "").toLowerCase().includes(s) || (p.project || "").toLowerCase().includes(s),
    );
  }, [q, ready, activeProject, selectedTitle]);

  if (!ready.length) {
    const proc = projects.find((p) => p.status === "processing");
    return (
      <div className="brdpick">
        <label>Choose a BRD to ground this chat</label>
        <div className="brdcombo">
          <input type="text" disabled dir="auto"
            placeholder={proc ? `A BRD is processing${typeof proc.progress === "number" ? ` — ${proc.progress}%` : "…"}${etaText(proc.eta_seconds) ? " · " + etaText(proc.eta_seconds) : ""}` : "No BRDs yet — add one via Manage BRDs"} />
        </div>
        <div className="hintline">
          {proc ? "It will appear here as soon as embedding finishes." : "Use “Manage BRDs” to add your first document."}
        </div>
      </div>
    );
  }

  const choose = (p: Project) => { onChoose(p.project); setQ(p.title || p.project); setOpen(false); setIdx(-1); };

  return (
    <div className="brdpick">
      <label>Choose a BRD to ground this chat</label>
      <div className="brdcombo">
        <input
          type="text"
          role="combobox"
          aria-expanded={open}
          dir="auto"
          placeholder="Search your BRDs…"
          value={q}
          onChange={(e) => { setQ(e.target.value); setIdx(-1); setOpen(true); }}
          onFocus={() => { setIdx(-1); setOpen(true); }}
          onBlur={() => setTimeout(() => setOpen(false), 120)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setIdx((i) => Math.min(i + 1, filtered.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(i - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); const pick = idx >= 0 ? filtered[idx] : filtered[0]; if (pick) choose(pick); }
            else if (e.key === "Escape") { setOpen(false); }
          }}
        />
        <div className={"brdopts" + (open ? " open" : "")}>
          {filtered.length === 0 ? (
            <div className="brdopt-empty">No matching BRDs</div>
          ) : (
            filtered.map((p, i) => (
              <div
                key={p.project}
                className={"brdopt" + (i === idx ? " active" : "")}
                dir="auto"
                onMouseDown={(e) => { e.preventDefault(); choose(p); }}
              >
                <div className="ot">{p.title || p.project}</div>
                <div className="sub">{p.project} · {p.docs} doc{p.docs === 1 ? "" : "s"}</div>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="hintline">Locks to this BRD once you send your first message.</div>
    </div>
  );
}
