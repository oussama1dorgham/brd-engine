import { useEffect, useMemo, useRef, useState } from "react";
import type { AttachEvent, ConversationMeta, Message, Project } from "./types";
import {
  askStream, attachStream, cancelAsk, deleteBrd, deleteConversation, getConversationsPage, getLlmKey, getLlmModels,
  getMessages, getProjects, getStarters, newConversation, renameBrd, resendVerification,
  setPreferredModels, uploadBrd, type AuthUser,
} from "./lib/api";
import { LOGO_SVG } from "./lib/constants";
import Sidebar from "./components/Sidebar";
import Composer from "./components/Composer";
import MessageBubble from "./components/MessageBubble";
import ScopePicker from "./components/ScopePicker";
import ModelPicker from "./components/ModelPicker";
import ModelsModal from "./components/ModelsModal";
import ManageBrdsModal from "./components/ManageBrdsModal";
import ConfirmModal, { type ConfirmState } from "./components/ConfirmModal";
import AccountSettings from "./components/AccountSettings";
import WelcomeState from "./components/WelcomeState";

type StreamOutcome = "idle" | "done" | "canceled" | "error" | "busy" | "empty" | "aborted";

// Patch the assistant bubble that's currently loading/streaming — not literally the
// last message, since a grayed "Queued" bubble may trail it.
function updStreaming(arr: Message[], patch: Partial<Message>): Message[] {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i].role === "assistant" && (arr[i].loading || arr[i].streaming)) {
      const copy = arr.slice();
      copy[i] = { ...copy[i], ...patch };
      return copy;
    }
  }
  return arr;
}

function EmptyState({
  activeProject, projLabel, onAsk,
}: {
  activeProject: string | null;
  projLabel: (p: string) => string;
  onAsk: (q: string) => void;
}) {
  const lbl = activeProject ? projLabel(activeProject) : null;
  const [starters, setStarters] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!activeProject) { setStarters(null); setLoading(false); return; }
    let cancelled = false;
    setLoading(true); setStarters(null);
    getStarters(activeProject).then((qs) => { if (!cancelled) { setStarters(qs); setLoading(false); } });
    return () => { cancelled = true; };
  }, [activeProject]);

  return (
    <div className="empty">
      <div className="empty-mark" dangerouslySetInnerHTML={{ __html: LOGO_SVG }} />
      <h2 className="empty-h">Ask {lbl ? `the ${lbl} BRD` : "your BRD"}, get grounded answers.</h2>
      <p className="empty-sub">
        Every reply is drawn only from the selected BRD and cites the requirement it came from — and it says so plainly
        when something isn't specified.
      </p>
      <div className="starters">
        {!activeProject ? (
          <div className="starters-hint">Choose a BRD from the selector at the top-right to begin.</div>
        ) : loading ? (
          <div className="starters-hint"><span className="typing"><i></i><i></i><i></i></span> Generating questions from the BRD…</div>
        ) : starters && starters.length ? (
          starters.map((t, i) => (
            <button key={i} className="chip" dir="auto" onClick={() => onAsk(t)}><span>{t}</span></button>
          ))
        ) : (
          <div className="starters-hint">Type your question below to get started.</div>
        )}
      </div>
    </div>
  );
}

// Kept off: verification is enforced up-front via OTP, so the post-login link banner
// isn't needed. Flip to true to re-enable a link-based "verify your email" nudge.
const SHOW_VERIFY_BANNER = false;

export default function App({ user, onLogout, verifiedNotice }: { user: AuthUser; onLogout: () => void; verifiedNotice?: boolean }) {
  const [theme, setTheme] = useState<string>(() => document.documentElement.getAttribute("data-theme") || "dark");
  const [resent, setResent] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [conversations, setConversations] = useState<ConversationMeta[]>([]);
  const [convHasMore, setConvHasMore] = useState(false);   // more older conversations to lazy-load
  const convLoadingRef = useRef(false);
  const [activeCid, setActiveCid] = useState<number | null>(null);
  const [activeProject, setActiveProject] = useState<string | null>(null);
  const [scopeLocked, setScopeLocked] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [toastMsg, setToastMsg] = useState<{ id: number; msg: string } | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [welcome, setWelcome] = useState(true);   // landing state: no conversation open
  const [logoWink, setLogoWink] = useState(false); // brief logo wink when a new chat starts
  const [llmConfigured, setLlmConfigured] = useState(false);  // BYOK key present
  const [allModels, setAllModels] = useState<string[]>([]);   // every model the key can reach
  const [preferred, setPreferred] = useState<string[]>([]);   // curated subset ([] = show all)
  const [model, setModel] = useState<string | null>(null);    // selected generation model
  const [modelsModalOpen, setModelsModalOpen] = useState(false);
  // What the in-chat picker offers: the curated set, or all models until curated.
  const pickModels = useMemo(() => (preferred.length ? preferred : allModels), [preferred, allModels]);
  const [composerFocus, setComposerFocus] = useState(0);
  const messagesRef = useRef<HTMLDivElement>(null);
  const activeCidRef = useRef<number | null>(null);      // latest activeCid, readable inside the async stream loop
  const streamingCidRef = useRef<number | null>(null);   // the conversation currently generating (on this client)
  const abortRef = useRef<AbortController | null>(null);  // cancels the in-flight /ask or attach stream
  const msgsRef = useRef<Message[]>([]);                 // latest messages, readable synchronously mid-stream
  const queuedRef = useRef<Map<number, string>>(new Map()); // one pending prompt per conversation
  const cancelingRef = useRef<number | null>(null);      // a cancel is in progress for this cid (it owns the queue promotion)
  const [, forceQueued] = useState(0);                   // re-render when the queue changes (drives composer state)

  useEffect(() => { activeCidRef.current = activeCid; }, [activeCid]);
  useEffect(() => { msgsRef.current = messages; }, [messages]);

  // Cancel the live stream (on navigation/refresh). The server keeps generating in
  // the background and persists the answer, so it reappears when the chat is reopened.
  const stopStreaming = () => {
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    streamingCidRef.current = null;
    setBusy(false);
  };

  const projTitle = useMemo(() => {
    const m: Record<string, string> = {};
    projects.forEach((p) => { m[p.project] = p.title || p.project; });
    return m;
  }, [projects]);
  const projLabel = (p: string) => projTitle[p] || p;

  const toast = (msg: string) => setToastMsg({ id: Date.now(), msg });
  useEffect(() => {
    if (!toastMsg) return;
    const t = setTimeout(() => setToastMsg(null), 2600);
    return () => clearTimeout(t);
  }, [toastMsg]);

  const askConfirm = (msg: string, yesLabel: string) =>
    new Promise<boolean>((resolve) => {
      setConfirm({ msg, yesLabel, resolve: (v) => { setConfirm(null); resolve(v); } });
    });

  const loadProjects = async () => { const ps = await getProjects(); setProjects(ps); return ps; };
  const refreshConvs = async () => {
    const page = await getConversationsPage();      // newest page; resets the lazy list
    setConversations(page.conversations);
    setConvHasMore(page.hasMore);
  };

  const loadMoreConvs = async () => {
    if (convLoadingRef.current || !convHasMore) return;
    const last = conversations[conversations.length - 1];
    if (!last) return;
    convLoadingRef.current = true;
    try {
      const page = await getConversationsPage(last.id);   // keyset cursor = last id seen
      setConversations((prev) => {
        const seen = new Set(prev.map((c) => c.id));
        return [...prev, ...page.conversations.filter((c) => !seen.has(c.id))];
      });
      setConvHasMore(page.hasMore);
    } finally {
      convLoadingRef.current = false;
    }
  };
  const closeSidebar = () => setSidebarOpen(false);

  const goHome = () => {
    stopStreaming();
    setSettingsOpen(false);
    setActiveCid(null);
    activeCidRef.current = null;
    setActiveProject(null);
    setMessages([]);
    setWelcome(true);
    closeSidebar();
  };

  const loadLlm = async () => {
    const meta = await getLlmKey().catch(() => null);
    const configured = !!(meta && meta.available && meta.configured);
    setLlmConfigured(configured);
    if (!configured) { setAllModels([]); setPreferred([]); setModel(null); return; }
    const { all, preferred: pref } = await getLlmModels().catch(() => ({ all: [] as string[], preferred: [] as string[] }));
    setAllModels(all);
    setPreferred(pref);
    const eff = pref.length ? pref : all;
    setModel((cur) => (cur && eff.includes(cur) ? cur : (eff[0] ?? null)));
  };

  // Persist the user's curated model list, then keep the selected model valid.
  const savePreferred = async (chosen: string[]): Promise<boolean> => {
    const r = await setPreferredModels(chosen).catch(() => ({ error: "Could not save models" }));
    if (!r || (r as { error?: string }).error) { toast((r as { error?: string })?.error || "Could not save models"); return false; }
    const pref = (r as { preferred?: string[] }).preferred || [];
    setPreferred(pref);
    const eff = pref.length ? pref : allModels;
    setModel((cur) => (cur && eff.includes(cur) ? cur : (eff[0] ?? null)));
    toast(pref.length ? `${pref.length} model${pref.length === 1 ? "" : "s"} selected ✓` : "Picker will show all models");
    return true;
  };

  // After a key is validated, refresh the model lists and prompt for a selection.
  const onKeySaved = async () => { await loadLlm(); setModelsModalOpen(true); };

  // --- queued-prompt helpers (one pending prompt per conversation) ---
  const askRef = useRef<(q: string) => void>(() => {});
  const bumpQueue = () => forceQueued((n) => n + 1);

  const enqueue = (cid: number, text: string) => {
    queuedRef.current.set(cid, text);
    try { localStorage.setItem("brd-queued-" + cid, text); } catch { /* ignore */ }
    if (activeCidRef.current === cid) setMessages((m) => [...m, { role: "user", content: text, queued: true }]);
    bumpQueue();
    toast("Queued — sends after the current answer");
  };

  const clearQueued = (cid: number) => {
    queuedRef.current.delete(cid);
    try { localStorage.removeItem("brd-queued-" + cid); } catch { /* ignore */ }
    bumpQueue();
  };

  // After a generation ends: free stream state and send any queued prompt for that chat.
  const flushQueue = (cid: number) => {
    const text = queuedRef.current.get(cid);
    if (text === undefined || activeCidRef.current !== cid) return;  // only auto-send while on screen
    clearQueued(cid);
    setMessages((m) => m.filter((x) => !x.queued));                  // drop the gray placeholder
    setTimeout(() => askRef.current(text), 0);                       // send for real once state settles
  };

  const finalizeGen = (cid: number) => {
    if (streamingCidRef.current === cid) { streamingCidRef.current = null; abortRef.current = null; }
    if (activeCidRef.current === cid) setBusy(false);
    if (cancelingRef.current === cid) return;   // a cancel is mid-flight — it will promote the queue itself
    flushQueue(cid);
  };

  // Shared consumer for both the POST /ask stream and the re-attach stream.
  const consumeStream = async (
    cid: number,
    gen: AsyncGenerator<AttachEvent>,
    controller: AbortController,
    opts: { needSetup: boolean; question?: string },
  ): Promise<StreamOutcome> => {
    const onView = () => activeCidRef.current === cid;
    // Only THIS request may patch the live bubble. After a cancel + new prompt, a
    // late event from the superseded stream must not clobber the new answer — its
    // controller is no longer the active one, so its patches are dropped.
    const patch = (p: Partial<Message>) => {
      if (onView() && abortRef.current === controller) setMessages((m) => updStreaming(m, p));
    };
    let question = opts.question ?? "";
    let ready = !opts.needSetup;
    let acc = "", started = false;
    let finalData: Extract<AttachEvent, { done: true }> | null = null;
    let outcome: StreamOutcome = "empty";

    // (attach path) render the pending turn once the server names the question
    const setup = (q: string) => {
      question = q;
      abortRef.current = controller;
      streamingCidRef.current = cid;
      setBusy(true);
      setMessages((m) => {
        const ph = m.find((x) => x.queued);
        const base = ph ? m.filter((x) => !x.queued) : m;
        const rows: Message[] = [...base, { role: "user", content: q }, { role: "assistant", content: "", loading: true }];
        return ph ? [...rows, ph] : rows;   // keep a queued placeholder trailing the pending turn
      });
      ready = true;
    };

    try {
      for await (const ev of gen) {
        if ("idle" in ev) return "idle";          // attach: nothing was running
        if ("busy" in ev) return "busy";          // 409: a prior turn is still finishing — caller retries
        if ("question" in ev) {
          const cur = msgsRef.current;
          const last = cur[cur.length - 1], prev = cur[cur.length - 2];
          if (last && last.role === "assistant" && !last.loading && !last.streaming && last.content &&
              prev && prev.role === "user" && prev.content === ev.question) {
            controller.abort();                   // that turn already persisted & shown — avoid a dup
            return "aborted";
          }
          setup(ev.question);
          continue;
        }
        if (!ready) continue;
        if ("t" in ev) { acc += ev.t; started = true; patch({ content: acc, loading: false, streaming: true }); }
        else if ("done" in ev) { finalData = ev; }
        else if ("canceled" in ev) { patch({ content: "canceled mate!!", loading: false, streaming: false, canceled: true }); outcome = "canceled"; }
        else if ("error" in ev) { patch({ content: "⚠ " + ev.error, loading: false, streaming: false, error: true }); if (onView()) toast(ev.error); outcome = "error"; }
      }
      if (outcome === "canceled" || outcome === "error") return outcome;
      if (finalData) {
        const fd = finalData;
        const cond = fd.standalone && fd.standalone.trim() !== question.trim() ? fd.standalone : undefined;
        patch({ content: fd.answer, sources: fd.sources, cond, loading: false, streaming: false });
        if (onView()) setScopeLocked(true);
        refreshConvs();
        return "done";
      }
      if (!started && ready) { patch({ content: "⚠ no response", loading: false, error: true }); return "error"; }
      return "empty";
    } catch (e) {
      if ((e as { name?: string })?.name === "AbortError") return "aborted";
      if (ready) patch({ content: "⚠ request failed", loading: false, streaming: false, error: true });
      if (onView()) toast("Request failed");
      return "error";
    }
  };

  // On opening a conversation: reconnect to an answer still being generated server-side.
  const resumeIfGenerating = async (cid: number) => {
    const controller = new AbortController();
    await consumeStream(cid, attachStream(cid, controller.signal), controller, { needSetup: true });
    if (streamingCidRef.current === cid) finalizeGen(cid);
  };

  // Stop the in-flight answer for the active chat: abort the stream, tell the server
  // to bail (nothing persisted), reflect "canceled mate!!", then start any queued prompt.
  const cancelGeneration = async () => {
    const cid = streamingCidRef.current;
    if (cid === null) return;
    cancelingRef.current = cid;   // finalizeGen must not promote the queue early (would get canceled too)
    streamingCidRef.current = null;
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    if (activeCidRef.current === cid) {
      setMessages((m) => updStreaming(m, { content: "canceled mate!!", loading: false, streaming: false, error: false, canceled: true }));
    }
    setBusy(false);
    await cancelAsk(cid);         // wait until the OLD generation is actually canceled server-side
    cancelingRef.current = null;
    flushQueue(cid);              // only now promote a queued prompt — it starts fresh and is abortable
  };

  const openConv = async (cid: number, project: string | null) => {
    // Leaving a different chat that's mid-stream cancels it (the server still saves it).
    if (streamingCidRef.current !== null && streamingCidRef.current !== cid) stopStreaming();
    setWelcome(false);
    setSettingsOpen(false);     // clicking a conversation always leaves settings/welcome
    setActiveCid(cid);
    activeCidRef.current = cid;  // sync now so the stream loop sees the switch immediately
    if (streamingCidRef.current === cid) { closeSidebar(); return; }  // this chat is streaming in-view — keep it live
    const msgs = await getMessages(cid);
    const hasMsgs = msgs.length > 0;
    setScopeLocked(!!project || hasMsgs);
    setActiveProject(project || null);
    // restore a queued prompt saved before a refresh, as a gray placeholder
    let queued: string | null = null;
    try { queued = localStorage.getItem("brd-queued-" + cid); } catch { /* ignore */ }
    const shown = queued ? [...msgs, { role: "user", content: queued, queued: true } as Message] : msgs;
    if (queued) queuedRef.current.set(cid, queued); else queuedRef.current.delete(cid);
    bumpQueue();
    msgsRef.current = shown;    // sync synchronously so the resume dedupe reads current state
    setMessages(shown);
    refreshConvs();
    closeSidebar();
    // reconnect to an in-progress answer (shows the bear + streams it in)
    await resumeIfGenerating(cid);
    // not generating but a prompt was queued from before → send it now
    if (streamingCidRef.current !== cid && queuedRef.current.has(cid)) flushQueue(cid);
  };

  // Lazy: opens a fresh empty chat but creates NO database row — the conversation
  // is persisted only when the first message is sent (see ask()). This stops empty
  // "New conversation" rows from piling up on every click.
  const newChat = () => {
    stopStreaming();
    setWelcome(false);
    setSettingsOpen(false);
    setLogoWink(true);
    setTimeout(() => setLogoWink(false), 1200);   // logo winks, then back to normal
    setActiveCid(null);
    activeCidRef.current = null;
    setActiveProject(null);
    setScopeLocked(false);
    setMessages([]);
    setComposerFocus((k) => k + 1);
    closeSidebar();
  };

  const deleteConv = async (cid: number) => {
    const ok = await askConfirm("this is a waste of tokens mate!!", "Delete now!!");
    if (!ok) return;
    if (streamingCidRef.current === cid) stopStreaming();  // don't keep streaming a deleted chat
    clearQueued(cid);                                       // drop any queued prompt for it
    await deleteConversation(cid);
    await refreshConvs();
    if (cid === activeCid) {
      // Return to the welcome state rather than auto-selecting another chat.
      setActiveCid(null);
      activeCidRef.current = null;
      setActiveProject(null);
      setMessages([]);
      setSettingsOpen(false);
      setWelcome(true);
    }
    toast("Conversation deleted");
  };

  const chooseProject = (p: string) => { setActiveProject(p); setComposerFocus((k) => k + 1); };

  const ask = async (question: string) => {
    const cid0 = activeCid;
    // If THIS conversation is already generating, queue the prompt instead of sending
    // (one at a time) — shown grayed, sent automatically when the current answer ends.
    if (cid0 !== null && streamingCidRef.current === cid0) {
      if (queuedRef.current.has(cid0)) { toast("Only one prompt can be queued"); return; }
      enqueue(cid0, question);
      return;
    }
    if (busy) return;

    let cid = cid0;
    if (cid === null) { cid = await newConversation(); setActiveCid(cid); activeCidRef.current = cid; refreshConvs(); }
    if (!scopeLocked && !activeProject) { toast("Choose a BRD to ground this chat first"); return; }

    streamingCidRef.current = cid;
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: question }, { role: "assistant", content: "", loading: true }]);

    // Send; if the server is still finishing a just-canceled turn (409 "busy"),
    // wait briefly and retry so a promoted queued prompt isn't rejected.
    let outcome: StreamOutcome = "busy";
    for (let attempt = 0; attempt < 12 && outcome === "busy"; attempt++) {
      if (attempt > 0) await new Promise((r) => setTimeout(r, 300));
      if (streamingCidRef.current !== cid) return;   // stopped / navigated while waiting
      const controller = new AbortController();
      abortRef.current = controller;
      outcome = await consumeStream(cid, askStream(question, cid, activeProject, model, controller.signal), controller, { needSetup: false, question });
    }
    if (outcome === "busy") {
      setMessages((m) => updStreaming(m, { content: "⚠ still finishing the previous answer — please try again", loading: false, streaming: false, error: true }));
    }
    finalizeGen(cid);
  };
  askRef.current = ask;   // let flushQueue() send the next queued prompt

  // --- Manage BRDs handlers ---
  const handleUpload = async (file: File, name: string): Promise<boolean> => {
    try {
      const d = await uploadBrd(file, name);
      await loadProjects();
      toast(`Uploaded “${d.title || d.project}” — processing in the background…`);
      return true;
    } catch (e) {
      toast("Upload failed: " + (e as Error).message);
      return false;
    }
  };
  const handleRename = async (project: string, title: string): Promise<boolean> => {
    try {
      const d = await renameBrd(project, title);
      if (d.error) throw new Error(d.error);
      await loadProjects();
      await refreshConvs();
      toast(`Renamed to “${title}”`);
      return true;
    } catch (e) {
      toast("Rename failed: " + (e as Error).message);
      return false;
    }
  };
  const handleDeleteBrd = async (project: string) => {
    const name = projTitle[project] || project;
    const ok = await askConfirm(`This permanently deletes “${name}” and all its chunks & embeddings. This cannot be undone.`, "End this BRD");
    if (!ok) return;
    try {
      const d = await deleteBrd(project);
      if (d.error) throw new Error(d.error);
      await loadProjects();
      if (activeProject === project && !scopeLocked) setActiveProject(null);
      toast(`Deleted “${name}”`);
    } catch (e) {
      toast("Delete failed: " + (e as Error).message);
    }
  };

  const toggleTheme = () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("brd-theme", next); } catch { /* ignore */ }
    setTheme(next);
  };

  const resend = async () => { await resendVerification(); setResent(true); toast("Verification email sent"); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (verifiedNotice) toast("Email verified ✓"); }, []);

  // scroll to bottom on new messages
  useEffect(() => { const el = messagesRef.current; if (el) el.scrollTop = el.scrollHeight; }, [messages]);

  // poll while any BRD is still embedding, so it flips to ready in the UI on its own
  useEffect(() => {
    if (!projects.some((p) => p.status === "processing")) return;
    const id = setInterval(() => { loadProjects(); }, 4000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects]);

  // "n" starts a new chat when not typing / no modal open
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (document.activeElement?.tagName || "").toLowerCase();
      if (e.key === "n" && !e.metaKey && !e.ctrlKey && tag !== "input" && tag !== "textarea" && !manageOpen && !confirm) {
        e.preventDefault();
        newChat();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manageOpen, confirm]);

  // initial load
  useEffect(() => {
    (async () => {
      await loadProjects();
      loadLlm();
      await refreshConvs();
      // Land on the welcome state — don't auto-open a conversation.
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ready = scopeLocked || !!activeProject;
  // While an answer is generating you may still type ONE next prompt (it gets queued);
  // once something is queued, the composer locks until it's sent.
  const hasQueuedActive = activeCid !== null && queuedRef.current.has(activeCid);
  const composerDisabled = !ready || (busy && hasQueuedActive);

  return (
    <>
      <div className={"scrim" + (sidebarOpen ? " show" : "")} onClick={closeSidebar} />
      <Sidebar
        open={sidebarOpen}
        theme={theme}
        conversations={conversations}
        activeCid={welcome || settingsOpen ? null : activeCid}
        projLabel={projLabel}
        onHome={goHome}
        logoWink={logoWink}
        onNewChat={newChat}
        onManage={() => { setManageOpen(true); closeSidebar(); }}
        onToggleTheme={toggleTheme}
        onOpenConv={openConv}
        onDeleteConv={deleteConv}
        onLoadMore={loadMoreConvs}
        hasMore={convHasMore}
        email={user.email}
        onLogout={onLogout}
        onOpenSettings={() => { stopStreaming(); setSettingsOpen(true); closeSidebar(); }}
      />
      <main>
        {/* Verification now happens up-front via a 6-digit code (blocking gate), so a
            logged-in user is already verified. This link-based banner is kept, hidden
            behind the flag, for a possible future link-verification mode. */}
        {SHOW_VERIFY_BANNER && !user.email_verified && (
          <div className="verifybar">
            <span>Verify your email — we sent a link to <b>{user.email}</b>.</span>
            <button onClick={resend} disabled={resent}>{resent ? "Sent ✓" : "Resend"}</button>
          </div>
        )}
        <div className="topbar">
          <button className="menu" aria-label="Toggle conversations" onClick={() => setSidebarOpen((v) => !v)}>&#9776;</button>
          <div className="hgrow">
            <h1>{settingsOpen ? "Account settings" : "Requirements Assistant"}</h1>
            <p>{settingsOpen
              ? "Manage your account."
              : "Answers grounded in one BRD — cited to the requirement, and honest about gaps."}</p>
          </div>
          {settingsOpen ? (
            <button className="backbtn" onClick={() => setSettingsOpen(false)}>← Back to chat</button>
          ) : welcome ? null : (
            <div className="topbar-tools">
              {llmConfigured && pickModels.length > 0 && (
                <ModelPicker
                  models={pickModels}
                  model={model}
                  onChoose={setModel}
                  onManage={() => setModelsModalOpen(true)}
                />
              )}
              <ScopePicker
                projects={projects}
                activeProject={activeProject}
                projTitle={projTitle}
                scopeLocked={scopeLocked}
                onChoose={chooseProject}
              />
            </div>
          )}
        </div>
        {settingsOpen ? (
          <AccountSettings
            email={user.email}
            toast={toast}
            onLlmChange={loadLlm}
            onKeySaved={onKeySaved}
            onManageModels={() => setModelsModalOpen(true)}
          />
        ) : welcome ? (
          <WelcomeState />
        ) : (
          <>
            <div className="messages" ref={messagesRef}>
              <div className="thread">
                {messages.length === 0 ? (
                  <EmptyState activeProject={activeProject} projLabel={projLabel} onAsk={ask} />
                ) : (
                  messages.map((m, i) => <MessageBubble key={i} m={m} />)
                )}
              </div>
            </div>
            <Composer
              disabled={composerDisabled}
              placeholder={
                !ready ? "Select a BRD to start chatting…"
                : busy && hasQueuedActive ? "A prompt is already queued…"
                : busy ? "Answering… your next prompt will be queued"
                : "Ask about the requirements…"
              }
              onSend={ask}
              focusKey={composerFocus}
              generating={busy}
              onStop={cancelGeneration}
            />
          </>
        )}
      </main>

      <ManageBrdsModal
        open={manageOpen}
        onClose={() => setManageOpen(false)}
        projects={projects}
        toast={toast}
        onUpload={handleUpload}
        onRename={handleRename}
        onDelete={handleDeleteBrd}
        confirmOpen={confirm !== null}
      />
      <ModelsModal
        open={modelsModalOpen}
        onClose={() => setModelsModalOpen(false)}
        all={allModels}
        preferred={preferred}
        onSave={savePreferred}
      />
      <ConfirmModal state={confirm} />
      <div className={"toast" + (toastMsg ? " show" : "")}>{toastMsg?.msg}</div>
    </>
  );
}
