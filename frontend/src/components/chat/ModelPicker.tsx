import { useEffect, useRef, useState } from "react";
import { getUsage } from "../../lib/api";

const CHECK_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

// Split a provider/model id into a short name + provider prefix, Claude-Desktop style.
function parts(id: string): { name: string; provider: string } {
  const i = id.indexOf("/");
  return i > 0 ? { name: id.slice(i + 1), provider: id.slice(0, i) } : { name: id, provider: "" };
}

/**
 * In-chat generation-model selector. A compact pill that opens a popover of the
 * user's curated models (Claude-Desktop feel), with a "Manage models…" action to
 * re-open the selection modal. Shown only when a BYOK key is configured.
 */
export default function ModelPicker({
  models, model, onChoose, onManage,
}: {
  models: string[];
  model: string | null;
  onChoose: (m: string) => void;
  onManage: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [left, setLeft] = useState<string | null>(null);   // "43 of 50 free requests left today"
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {                                       // server-cached for a minute: cheap on every open
    if (!open) return;
    getUsage().then((u) => {
      const f = u.uses === "own" ? u.own_key?.free_daily : u.system?.free_daily;
      setLeft(f && f.limit != null ? `${f.remaining ?? 0} of ${f.limit} free requests left today` : null);
    }).catch(() => setLeft(null));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const cur = model ? parts(model) : null;

  return (
    <div className="modelwrap" ref={wrapRef}>
      <button
        className="modelpill"
        title="Generation model"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="modelpill-name">{cur ? cur.name : "Select model"}</span>
        <span className="scopecaret" aria-hidden="true" />
      </button>
      {open && (
        <div className="modelmenu" role="listbox">
          <div className="modellist-pop">
            {models.map((m) => {
              const p = parts(m);
              const active = m === model;
              return (
                <div
                  key={m}
                  role="option"
                  aria-selected={active}
                  className={"modelopt" + (active ? " active" : "")}
                  dir="auto"
                  onMouseDown={(e) => { e.preventDefault(); onChoose(m); setOpen(false); }}
                >
                  <span className="modelopt-check" aria-hidden="true"
                    dangerouslySetInnerHTML={active ? { __html: CHECK_SVG } : { __html: "" }} />
                  <span className="modelopt-text">
                    <span className="ot">{p.name}</span>
                    {p.provider && <span className="sub">{p.provider}</span>}
                  </span>
                </div>
              );
            })}
          </div>
          {left && <div className="modelusage" title="Free-model requests your key has left today (resets daily)">{left}</div>}
          <button className="modelmanage" onMouseDown={(e) => { e.preventDefault(); setOpen(false); onManage(); }}>
            Manage models…
          </button>
        </div>
      )}
    </div>
  );
}
