import { useEffect, useRef, useState } from "react";

// Landing view: the BRDS mascot — a brown bear with a "QA" karate bandana and
// glasses. He winks + waves on arrival, reacts when clicked (wink / page-flip),
// and gets visibly bored (eye-roll + open mouth) on every third click.

const QUOTES = [
  "Hello, Mr QA 👋",
  "Ready to interrogate some BRDs?",
  "Pick a BRD — I'll cite chapter and verse.",
  "Requirements don't stand a chance.",
  "I read the fine print so you don't have to.",
  "Let's hunt that missing requirement.",
  "Ask me anything — grounded, of course.",
];
const BORED_QUOTES = [
  "Again? I'm working here.",
  "*rolls eyes* yes, still reading…",
  "You done poking me?",
  "I have BRDs to review, you know.",
  "Bored yet? Because I am.",
];
const pick = (list: string[], cur: string) => {
  const o = list.filter((x) => x !== cur);
  return o[Math.floor(Math.random() * o.length)] ?? list[0];
};

export default function WelcomeState() {
  const [quote, setQuote] = useState(QUOTES[0]);
  const [pop, setPop] = useState(0);          // remount key → replays bubble entrance
  const [winking, setWinking] = useState(false);
  const [flipping, setFlipping] = useState(false);
  const [bored, setBored] = useState(false);
  const [greeting, setGreeting] = useState(true);
  const [readPhase, setReadPhase] = useState<"closed" | "open" | "flip" | "close">("closed");
  const clicks = useRef(0);

  useEffect(() => {
    const on = setTimeout(() => setWinking(true), 450);
    const off = setTimeout(() => setWinking(false), 1150);
    const done = setTimeout(() => setGreeting(false), 1700);
    return () => { clearTimeout(on); clearTimeout(off); clearTimeout(done); };
  }, []);

  const react = () => {
    clicks.current += 1;
    if (clicks.current % 3 === 0) {           // every third click → bored/annoyed
      setWinking(false); setFlipping(false);
      setQuote((q) => pick(BORED_QUOTES, q));
      setPop((p) => p + 1);
      setBored(true);
      setTimeout(() => setBored(false), 1800);
      return;
    }
    setQuote((q) => pick(QUOTES, q));
    setPop((p) => p + 1);
    if (Math.random() < 0.5) {
      setWinking(true); setTimeout(() => setWinking(false), 700);
    } else {
      setFlipping(true); setTimeout(() => setFlipping(false), 600);
    }
  };

  // Clicking the BOOK (not the bear): open it, lower the glasses, flip the pages,
  // then close it. stopPropagation so the bear's own reaction doesn't also fire.
  const startReading = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (readPhase !== "closed") return;
    setReadPhase("open");
    setTimeout(() => setReadPhase("flip"), 450);
    setTimeout(() => setReadPhase("close"), 2100);
    setTimeout(() => setReadPhase("closed"), 2650);
  };
  const reading = readPhase !== "closed";
  const showOpen = readPhase === "open" || readPhase === "flip";

  return (
    <div className="welcome">
      <div className="welcome-scene">
        <button className="welcome-bearbtn" onClick={react} aria-label="Say hi to the BRDS bear">
          <svg className="welcome-bear" viewBox="0 0 200 176" aria-hidden="true"
               fontFamily="ui-sans-serif, system-ui, sans-serif">
            <defs>
              <linearGradient id="furG" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#c78a4f" />
                <stop offset="1" stopColor="#a4672f" />
              </linearGradient>
              <linearGradient id="bookG" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#ffab33" />
                <stop offset="1" stopColor="#f08a00" />
              </linearGradient>
            </defs>

            {/* bandana tails (behind head) — tapered cloth ribbons with forked
                (swallowtail) ends, each fluttering independently in the wind */}
            <g className="welcome-tails">
              <path className="tail tail-1" fill="var(--bandana)"
                    d="M50,39 Q30,39 12,46 L21,51 L11,57 Q31,54 50,48 Z" />
              <path className="tail tail-1b" fill="#000000" opacity="0.16"
                    d="M50,44 Q32,45 14,51 L22,54 L50,48 Z" />
              <path className="tail tail-2" fill="var(--bandana)"
                    d="M48,47 Q31,51 15,59 L24,63 L14,69 Q31,61 47,54 Z" />
            </g>

            {/* ears */}
            <circle cx="56" cy="30" r="18" fill="#8a5a2b" />
            <circle cx="144" cy="30" r="18" fill="#8a5a2b" />
            <circle cx="56" cy="30" r="9" fill="#c98f52" />
            <circle cx="144" cy="30" r="9" fill="#c98f52" />

            {/* head + soft highlight + cheeks */}
            <rect x="38" y="20" width="124" height="100" rx="48" fill="url(#furG)" />
            <ellipse cx="100" cy="44" rx="46" ry="18" fill="#ffffff" opacity="0.07" />
            <ellipse cx="71" cy="96" rx="9" ry="6" fill="#d9705a" opacity="0.22" />
            <ellipse cx="129" cy="96" rx="9" ry="6" fill="#d9705a" opacity="0.22" />

            {/* QA karate bandana (band + text, in front) — theme-aware. The tie is
                read from the rounded band end + the tails, so no separate knot dot. */}
            <g fill="var(--bandana)">
              <rect x="43" y="32" width="114" height="15" rx="7" />
            </g>
            <text x="102" y="44" textAnchor="middle" fontSize="14" fontWeight="800" fill="var(--bandana-ink)"
                  letterSpacing="1.5">QA</text>

            {/* muzzle + nose */}
            <ellipse cx="100" cy="92" rx="26" ry="19" fill="#f0dcbf" />
            <ellipse cx="100" cy="84" rx="7" ry="5" fill="#2b2b2b" />
            <circle cx="97.5" cy="82" r="1.6" fill="#ffffff" opacity="0.6" />

            {/* eye whites */}
            <circle cx="78" cy="64" r="16" fill="#ffffff" />
            {winking
              ? <path d="M113 66 Q122 73 131 66" fill="none" stroke="#222" strokeWidth="4.5" strokeLinecap="round" />
              : <circle cx="122" cy="64" r="16" fill="#ffffff" />}

            {/* pupils — normal, winking (left only), or bored (both, rolling) */}
            {winking ? (
              <>
                <circle cx="81" cy="66" r="6.5" fill="#222" /><circle cx="83" cy="63" r="2" fill="#fff" />
              </>
            ) : bored ? (
              <g className="eyeroll">
                <circle cx="81" cy="64" r="6.5" fill="#222" />
                <circle cx="119" cy="64" r="6.5" fill="#222" />
              </g>
            ) : (
              <>
                <circle cx="81" cy="66" r="6.5" fill="#222" /><circle cx="83" cy="63" r="2" fill="#fff" />
                <circle cx="119" cy="66" r="6.5" fill="#222" /><circle cx="121" cy="63" r="2" fill="#fff" />
              </>
            )}

            {/* glasses + lens shine (drop down the snout while reading) */}
            <g className={"welcome-glasses" + (reading ? " is-down" : "")}>
              <g fill="none" stroke="#2b2b2b" strokeWidth="4.5" strokeLinecap="round">
                <circle cx="78" cy="64" r="20" />
                <circle cx="122" cy="64" r="20" />
                <line x1="96" y1="62" x2="104" y2="62" />
                <line x1="58" y1="60" x2="44" y2="53" />
                <line x1="142" y1="60" x2="156" y2="53" />
              </g>
              <g stroke="#ffffff" strokeWidth="3" strokeLinecap="round" opacity="0.55">
                <line x1="70" y1="58" x2="77" y2="54" />
                <line x1="114" y1="58" x2="121" y2="54" />
              </g>
            </g>

            {/* mouth — smile, or open + annoyed when bored */}
            {bored ? (
              <>
                <ellipse cx="100" cy="101" rx="9" ry="7.5" fill="#3a241a" />
                <ellipse cx="100" cy="104" rx="5" ry="3" fill="#c96a5a" />
              </>
            ) : (
              <path d="M92 96 Q100 104 108 96" fill="none" stroke="#2b2b2b" strokeWidth="3" strokeLinecap="round" />
            )}

            {/* book (tilted): click it to read — opens, flips pages, closes */}
            <g transform="rotate(-6 100 134)">
              {showOpen ? (
                <g className="welcome-openbook">
                  <rect x="50" y="113" width="100" height="48" rx="6" fill="#e08600" />
                  <rect x="54" y="116" width="45" height="42" rx="3" fill="#fff8ec" />
                  <rect x="101" y="116" width="45" height="42" rx="3" fill="#fff8ec" />
                  <rect x="98" y="114" width="4" height="46" fill="#c9720a" />
                  <g stroke="#d8cdb5" strokeWidth="2" strokeLinecap="round">
                    <line x1="60" y1="124" x2="92" y2="124" /><line x1="60" y1="132" x2="92" y2="132" />
                    <line x1="60" y1="140" x2="92" y2="140" /><line x1="60" y1="148" x2="88" y2="148" />
                    <line x1="108" y1="124" x2="140" y2="124" /><line x1="108" y1="132" x2="140" y2="132" />
                    <line x1="108" y1="140" x2="140" y2="140" /><line x1="108" y1="148" x2="136" y2="148" />
                  </g>
                  {readPhase === "flip" && (
                    <rect className="bookpage-flip" x="100" y="116" width="45" height="42" rx="3"
                          fill="#fdf5e4" stroke="#e7dcc4" strokeWidth="1" />
                  )}
                </g>
              ) : (
                <g className={"welcome-book" + (flipping ? " is-flip" : "")}>
                  <rect x="139" y="114" width="9" height="46" rx="2" fill="#f3e7cf" />
                  <rect x="52" y="112" width="90" height="48" rx="6" fill="url(#bookG)" stroke="#c9720a" strokeWidth="2" />
                  <rect x="52" y="112" width="10" height="48" rx="5" fill="#c9720a" />
                  <rect x="66" y="118" width="70" height="36" rx="4" fill="none" stroke="#ffffff" strokeWidth="1.3" opacity="0.5" />
                  <text x="101" y="129" textAnchor="middle" fontSize="6.5" fontWeight="700" fill="#fff" opacity="0.85"
                        letterSpacing="1.2">REQUIREMENTS</text>
                  <text x="101" y="145" textAnchor="middle" fontSize="19" fontWeight="800" fill="#ffffff"
                        letterSpacing="1.5">BRDS</text>
                  <line x1="83" y1="150" x2="119" y2="150" stroke="#fff" strokeWidth="1.4" opacity="0.7" />
                </g>
              )}
              {/* transparent hit area — clicking the book starts the reading sequence */}
              <rect x="50" y="110" width="100" height="52" fill="transparent" pointerEvents="all"
                    style={{ cursor: "pointer" }} onClick={startReading} />
            </g>

            {/* left paw always holds the book; right paw waves during the greeting */}
            <circle cx="54" cy="126" r="15" fill="#b5793f" stroke="#8a5a2b" strokeWidth="2" />
            {greeting ? (
              <g className="welcome-wave">
                <rect x="150" y="86" width="14" height="40" rx="7" fill="#b5793f" stroke="#8a5a2b" strokeWidth="2" />
                <circle cx="157" cy="84" r="14" fill="#b5793f" stroke="#8a5a2b" strokeWidth="2" />
              </g>
            ) : (
              <circle cx="146" cy="126" r="15" fill="#b5793f" stroke="#8a5a2b" strokeWidth="2" />
            )}
          </svg>
        </button>

        <div className="welcome-bubble" key={pop}>{quote}</div>
      </div>
      <p className="welcome-sub">Click the bear, pick a conversation, or start a new chat to begin.</p>
    </div>
  );
}
