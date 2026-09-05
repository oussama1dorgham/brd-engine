import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

// A styled hover tooltip rendered in a portal at <body> with position:fixed, so it
// lives OUTSIDE any scrollable/overflow container — it can't be clipped and can't
// flicker a scrollbar (the failure mode of a CSS ::after tooltip inside .messages).
export default function Tip({ text, className, children }: { text: string; className?: string; children: ReactNode }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [tip, setTip] = useState<{ left: number; top: number; flip: boolean } | null>(null);

  const show = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const flip = r.right + 340 > window.innerWidth; // not enough room on the right → open left
    setTip({ left: flip ? r.left - 10 : r.right + 10, top: r.top + r.height / 2, flip });
  };
  const hide = () => setTip(null);

  useEffect(() => {
    if (!tip) return;
    const onScroll = () => hide();
    window.addEventListener("scroll", onScroll, true);
    return () => window.removeEventListener("scroll", onScroll, true);
  }, [tip]);

  return (
    <span ref={ref} className={className} onMouseEnter={show} onMouseLeave={hide}>
      {children}
      {tip &&
        createPortal(
          <div
            className="floattip"
            dir="auto"
            style={{ left: tip.left, top: tip.top, transform: tip.flip ? "translate(-100%,-50%)" : "translateY(-50%)" }}
          >
            {text}
          </div>,
          document.body,
        )}
    </span>
  );
}
