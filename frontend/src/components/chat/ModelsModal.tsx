import { useEffect, useMemo, useRef, useState } from "react";

const SEARCH_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>';
const CHECK_SVG =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

/**
 * "Choose your models" — opens right after a key is validated (and re-openable
 * later). Lists every model the key can reach; the user ticks the ones they want
 * in the in-chat picker. Search is hidden behind the icon: the bar only appears
 * once the search icon is clicked, then filters the list live.
 */
export default function ModelsModal({
  open, onClose, all, preferred, onSave,
}: {
  open: boolean;
  onClose: () => void;
  all: string[];
  preferred: string[];
  onSave: (models: string[]) => Promise<boolean>;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const [searchOn, setSearchOn] = useState(false);
  const [saving, setSaving] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  // Re-seed each time the modal opens from the current preferred set.
  useEffect(() => {
    if (open) { setSelected(new Set(preferred)); setQ(""); setSearchOn(false); }
  }, [open, preferred]);

  useEffect(() => { if (searchOn) setTimeout(() => searchRef.current?.focus(), 30); }, [searchOn]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !saving) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, saving, onClose]);

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    return s ? all.filter((m) => m.toLowerCase().includes(s)) : all;
  }, [q, all]);

  if (!open) return null;

  const toggle = (m: string) =>
    setSelected((prev) => { const n = new Set(prev); n.has(m) ? n.delete(m) : n.add(m); return n; });

  const save = async () => {
    if (saving) return;
    setSaving(true);
    const ok = await onSave(Array.from(selected));
    setSaving(false);
    if (ok) onClose();
  };

  const count = selected.size;

  return (
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget && !saving) onClose(); }}>
      <div className="modal modelsmodal" role="dialog" aria-modal="true" aria-labelledby="modelsmodal-title">
        <div className="modal-head">
          <h2 id="modelsmodal-title">Choose your models</h2>
          <button
            className={"modelsearch-btn" + (searchOn ? " on" : "")}
            aria-label={searchOn ? "Hide search" : "Search models"}
            aria-pressed={searchOn}
            title="Search models"
            onClick={() => { setSearchOn((v) => { if (v) setQ(""); return !v; }); }}
            dangerouslySetInnerHTML={{ __html: SEARCH_SVG }}
          />
          <button className="modal-x" aria-label="Close" onClick={() => { if (!saving) onClose(); }}>✕</button>
        </div>

        {searchOn && (
          <div className="modelsearch-bar">
            <input
              ref={searchRef}
              type="text"
              dir="auto"
              placeholder="Filter models…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        )}

        <div className="models-toolbar">
          <span className="models-count">{count} of {all.length} selected</span>
          <div className="models-toolbar-actions">
            <button className="linkbtn" onClick={() => setSelected(new Set(all))}>Select all</button>
            <button className="linkbtn" onClick={() => setSelected(new Set())}>Clear</button>
          </div>
        </div>

        <div className="modal-body models-body">
          {all.length === 0 ? (
            <div className="brdempty">No models available for this key.</div>
          ) : filtered.length === 0 ? (
            <div className="brdempty">No models match “{q}”.</div>
          ) : (
            <div className="modellist">
              {filtered.map((m) => {
                const on = selected.has(m);
                return (
                  <button
                    key={m}
                    type="button"
                    className={"modelrow" + (on ? " on" : "")}
                    role="checkbox"
                    aria-checked={on}
                    onClick={() => toggle(m)}
                    dir="auto"
                  >
                    <span className="modelcheck" aria-hidden="true"
                      dangerouslySetInnerHTML={on ? { __html: CHECK_SVG } : { __html: "" }} />
                    <span className="modelname">{m}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="models-foot">
          <p className="models-hint">
            {count === 0
              ? "Nothing selected — the picker will show all models."
              : "These appear in the model picker at the top of a chat."}
          </p>
          <button className="primary models-save" disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save selection"}
          </button>
        </div>
      </div>
    </div>
  );
}
