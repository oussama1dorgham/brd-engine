import { useEffect, useState } from "react";
import { proofUrl, type UcTestProof } from "../../lib/api";

// Full-size viewer for image/video evidence: Esc closes, ←/→ step through the event's media.
export default function ProofLightbox({ items, index, onClose }: {
  items: UcTestProof[]; index: number; onClose: () => void;
}) {
  const [i, setI] = useState(index);
  const p = items[i];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowRight") setI((n) => Math.min(items.length - 1, n + 1));
      else if (e.key === "ArrowLeft") setI((n) => Math.max(0, n - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items.length, onClose]);

  if (!p) return null;
  const label = p.caption || p.filename || "evidence";
  return (
    <div className="uc-lightbox" onClick={onClose} role="dialog" aria-label={label}>
      <div className="uc-lightbox-body" onClick={(e) => e.stopPropagation()}>
        {p.kind === "video"
          ? <video src={proofUrl(p.id)} controls autoPlay />
          : <img src={proofUrl(p.id)} alt={label} />}
        <div className="uc-lightbox-bar">
          <span className="uc-lightbox-name">{label}</span>
          {items.length > 1 && <span className="uc-lightbox-n">{i + 1} / {items.length}</span>}
          <button disabled={i === 0} onClick={() => setI(i - 1)} title="Previous (←)">‹</button>
          <button disabled={i === items.length - 1} onClick={() => setI(i + 1)} title="Next (→)">›</button>
          <a href={proofUrl(p.id, true)} title="Download">⤓</a>
          <button onClick={onClose} title="Close (Esc)">✕</button>
        </div>
      </div>
    </div>
  );
}
