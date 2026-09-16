import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Message, Source } from "../types";
import { answerHtml, srcFull, srcLabel } from "../lib/markdown";
import { snippet as citeSnippet } from "../lib/cite";
import { getRequirementTitles } from "../lib/api";
import { LOADING, LOGO_WINK_SVG } from "../lib/constants";
import Tip from "./Tip";

// The answer HTML contains inline [n] citation spans (.cite[data-tip]). A CSS ::after
// tooltip on those lives INSIDE the scrollable .messages container, so near the bottom it
// grows the scroll height and the page goes shaky. Render the tip in a fixed portal at
// <body> instead (like Tip does for source chips) via one delegated hover handler.
function CitedAnswer({ html }: { html: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<{ text: string; left: number; top: number; flip: boolean } | null>(null);

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    let current: HTMLElement | null = null;
    const cite = (t: EventTarget | null) =>
      (t as HTMLElement)?.closest?.(".cite[data-tip]") as HTMLElement | null;
    const onOver = (e: Event) => {
      const t = cite(e.target);
      if (!t || t === current) return;
      current = t;
      const r = t.getBoundingClientRect();
      const flip = r.right + 340 > window.innerWidth;   // not enough room right → open left
      setTip({
        text: t.getAttribute("data-tip") || "",
        left: flip ? r.left - 10 : r.right + 10,
        top: r.top + r.height / 2,
        flip,
      });
    };
    const onOut = (e: Event) => {
      if (cite(e.target) === current) { current = null; setTip(null); }
    };
    root.addEventListener("mouseover", onOver);
    root.addEventListener("mouseout", onOut);
    return () => { root.removeEventListener("mouseover", onOver); root.removeEventListener("mouseout", onOut); };
  }, [html]);

  useEffect(() => {
    if (!tip) return;
    const onScroll = () => setTip(null);
    window.addEventListener("scroll", onScroll, true);
    return () => window.removeEventListener("scroll", onScroll, true);
  }, [tip]);

  return (
    <>
      <div ref={ref} className="bubble md" dir="auto" dangerouslySetInnerHTML={{ __html: html }} />
      {tip && createPortal(
        <div className="floattip" dir="auto"
             style={{ left: tip.left, top: tip.top, transform: tip.flip ? "translate(-100%,-50%)" : "translateY(-50%)" }}>
          {tip.text}
        </div>,
        document.body,
      )}
    </>
  );
}

function CopyBtn({ text }: { text: string }) {
  const [label, setLabel] = useState("Copy");
  return (
    <button
      className="copy"
      onClick={() =>
        navigator.clipboard.writeText(text).then(() => {
          setLabel("Copied");
          setTimeout(() => setLabel("Copy"), 1200);
        })
      }
    >
      {label}
    </button>
  );
}

function Sources({ sources }: { sources: Source[] }) {
  const [titles, setTitles] = useState<Record<number, string>>({});
  useEffect(() => {
    const byProject: Record<string, number[]> = {};
    for (const s of sources) if (s.chunk_id != null) (byProject[s.project] ||= []).push(s.chunk_id);
    if (Object.keys(byProject).length === 0) return;
    let cancelled = false;
    Promise.all(Object.entries(byProject).map(([p, ids]) => getRequirementTitles(p, ids)))
      .then((maps) => {
        if (cancelled) return;
        const add: Record<number, string> = {};
        for (const t of maps) for (const k in t) if (t[k]) add[Number(k)] = t[k];
        if (Object.keys(add).length) setTitles(add);
      })
      .catch(() => { /* fail-open: keep fallback labels */ });
    return () => { cancelled = true; };
  }, [sources]);
  const tipFor = (x: Source) => {
    const t = x.chunk_id != null ? titles[x.chunk_id] : undefined;
    if (!t) return srcFull(x);
    const body = citeSnippet(x.full || x.snippet || "", 200);
    return body ? `${t} — ${body}  ·  ${x.project}` : `${t}  ·  ${x.project}`;
  };
  return (
    <div className="sources">
      {sources.map((x) => (
        <Tip key={x.n} text={tipFor(x)} className="src">
          <span className="src-t" dir="auto">
            [{x.n}] {(x.chunk_id != null && titles[x.chunk_id]) || srcLabel(x)} · {x.project}
          </span>
        </Tip>
      ))}
    </div>
  );
}

function BotBubble({ m }: { m: Message }) {
  const [phrase, setPhrase] = useState(() => LOADING[Math.floor(Math.random() * LOADING.length)]);
  useEffect(() => {
    if (!m.loading) return;
    const id = setInterval(() => setPhrase(LOADING[Math.floor(Math.random() * LOADING.length)]), 1900);
    return () => clearInterval(id);
  }, [m.loading]);

  if (m.loading) {
    return (
      <div className="bubble md bare">
        <span className="load">
          <span className="loadbear" dangerouslySetInnerHTML={{ __html: LOGO_WINK_SVG }} />
          <span className="lt">{phrase}…</span>
        </span>
      </div>
    );
  }
  if (m.streaming) {
    return <div className="bubble streaming" dir="auto">{m.content}</div>;
  }
  if (m.canceled) {
    return <div className="bubble canceled" dir="auto">{m.content}</div>;
  }
  if (m.error) {
    return <div className="bubble" dir="auto">{m.content}</div>;
  }
  return <CitedAnswer html={answerHtml(m.content, m.sources)} />;
}

export default function MessageBubble({ m }: { m: Message }) {
  if (m.role === "user") {
    return (
      <div className={"msg user" + (m.queued ? " queued" : "")}>
        <div className="row">
          <div className="bubble" dir="auto">{m.content}</div>
          {m.queued && <span className="queued-badge" title="Waiting for the current answer to finish">Queued</span>}
        </div>
      </div>
    );
  }
  const settled = !m.loading && !m.streaming && !m.error && !m.canceled;
  return (
    <div className="msg bot">
      <div className="row">
        <BotBubble m={m} />
        {settled && <CopyBtn text={m.content} />}
        {m.cond && <div className="cond">interpreted as: {m.cond}</div>}
        {m.sources && m.sources.length > 0 && <Sources sources={m.sources} />}
      </div>
    </div>
  );
}
