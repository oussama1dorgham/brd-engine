import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";

/**
 * Mouse-drag horizontal resizing for a panel, persisted per `storageKey`.
 *
 * Returns the current pixel `size`, an `onDragStart` to wire to a drag handle's
 * onMouseDown, and a `reset` to restore the default. The value is clamped to
 * [min, max] and remembered in localStorage across reloads.
 *
 * `edge` says which side of the panel the handle lives on: "right" (default)
 * grows the panel as the mouse moves right (sidebar / left-hand tree); "left"
 * grows it as the mouse moves left (a right-hand panel).
 */
export function useResizable(opts: {
  storageKey: string;
  initial: number;
  min: number;
  max: number;
  edge?: "left" | "right";
}) {
  const { storageKey, initial, min, max, edge = "right" } = opts;
  const clamp = (n: number) => Math.min(max, Math.max(min, n));

  const [size, setSize] = useState<number>(() => {
    try {
      const v = localStorage.getItem(storageKey);
      if (v != null) { const n = parseInt(v, 10); if (!Number.isNaN(n)) return clamp(n); }
    } catch { /* private mode / blocked storage — fall through to default */ }
    return clamp(initial);
  });

  const sizeRef = useRef(size);
  sizeRef.current = size;                      // keep latest for drag-base + persistence
  const startRef = useRef<{ x: number; base: number } | null>(null);
  const moveRef = useRef<((e: MouseEvent) => void) | null>(null);
  const upRef = useRef<(() => void) | null>(null);

  const onDragStart = (e: ReactMouseEvent) => {
    e.preventDefault();
    startRef.current = { x: e.clientX, base: sizeRef.current };
    document.body.classList.add("resizing");

    const move = (ev: MouseEvent) => {
      const s = startRef.current;
      if (!s) return;
      const delta = ev.clientX - s.x;
      setSize(clamp(edge === "right" ? s.base + delta : s.base - delta));
    };
    const up = () => {
      startRef.current = null;
      document.body.classList.remove("resizing");
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      try { localStorage.setItem(storageKey, String(Math.round(sizeRef.current))); } catch { /* ignore */ }
    };

    moveRef.current = move;
    upRef.current = up;
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  useEffect(() => () => {   // safety net: drop listeners if unmounted mid-drag
    if (moveRef.current) window.removeEventListener("mousemove", moveRef.current);
    if (upRef.current) window.removeEventListener("mouseup", upRef.current);
  }, []);

  return { size, onDragStart, reset: () => setSize(clamp(initial)) };
}
