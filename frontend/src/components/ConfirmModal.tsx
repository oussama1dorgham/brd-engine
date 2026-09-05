import { useEffect, useRef } from "react";

export interface ConfirmState {
  msg: string;
  yesLabel: string;
  resolve: (v: boolean) => void;
}

const WARN_SVG =
  '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

export default function ConfirmModal({ state }: { state: ConfirmState | null }) {
  const noRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!state) return;
    const t = setTimeout(() => noRef.current?.focus(), 40);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); state.resolve(false); }
    };
    document.addEventListener("keydown", onKey);
    return () => { clearTimeout(t); document.removeEventListener("keydown", onKey); };
  }, [state]);

  if (!state) return null;
  return (
    <div className="modal-scrim confirm-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) state.resolve(false); }}>
      <div className="modal confirm" role="alertdialog" aria-modal="true">
        <div className="confirm-body">
          <div className="warnicon" aria-hidden="true" dangerouslySetInnerHTML={{ __html: WARN_SVG }} />
          <h2>Are you sure about that?</h2>
          <p className="confirm-msg">{state.msg}</p>
        </div>
        <div className="confirm-actions">
          <button className="btn-ghost" ref={noRef} onClick={() => state.resolve(false)}>Hell nah</button>
          <button className="btn-danger" onClick={() => state.resolve(true)}>{state.yesLabel}</button>
        </div>
      </div>
    </div>
  );
}
