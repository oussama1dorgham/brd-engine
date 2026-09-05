import { useEffect, useState } from "react";
import type { Message, Source } from "../types";
import { answerHtml, srcFull, srcLabel } from "../lib/markdown";
import { LOADING, LOGO_WINK_SVG } from "../lib/constants";
import Tip from "./Tip";

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
  return (
    <div className="sources">
      {sources.map((x) => (
        <Tip key={x.n} text={srcFull(x)} className="src">
          <span className="src-t" dir="auto">
            [{x.n}] {srcLabel(x)} · {x.project}
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
  if (m.error) {
    return <div className="bubble" dir="auto">{m.content}</div>;
  }
  return <div className="bubble md" dir="auto" dangerouslySetInnerHTML={{ __html: answerHtml(m.content, m.sources) }} />;
}

export default function MessageBubble({ m }: { m: Message }) {
  if (m.role === "user") {
    return (
      <div className="msg user">
        <div className="row">
          <div className="bubble" dir="auto">{m.content}</div>
        </div>
      </div>
    );
  }
  const settled = !m.loading && !m.streaming && !m.error;
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
