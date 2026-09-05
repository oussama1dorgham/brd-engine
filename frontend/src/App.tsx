import { useEffect, useMemo, useRef, useState } from "react";
import type { ConversationMeta, Message, Project } from "./types";
import {
  askStream, deleteBrd, deleteConversation, getConversations, getMessages,
  getProjects, getStarters, newConversation, renameBrd, resendVerification,
  uploadBrd, type AuthUser,
} from "./lib/api";
import { LOGO_SVG } from "./lib/constants";
import Sidebar from "./components/Sidebar";
import Composer from "./components/Composer";
import MessageBubble from "./components/MessageBubble";
import ScopePicker from "./components/ScopePicker";
import ManageBrdsModal from "./components/ManageBrdsModal";
import ConfirmModal, { type ConfirmState } from "./components/ConfirmModal";
import AccountSettings from "./components/AccountSettings";
import WelcomeState from "./components/WelcomeState";

function updLast(arr: Message[], patch: Partial<Message>): Message[] {
  if (!arr.length) return arr;
  const copy = arr.slice();
  copy[copy.length - 1] = { ...copy[copy.length - 1], ...patch };
  return copy;
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
  const [composerFocus, setComposerFocus] = useState(0);
  const messagesRef = useRef<HTMLDivElement>(null);

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
  const refreshConvs = async () => { setConversations(await getConversations()); };
  const closeSidebar = () => setSidebarOpen(false);

  const goHome = () => {
    setSettingsOpen(false);
    setActiveCid(null);
    setActiveProject(null);
    setMessages([]);
    setWelcome(true);
    closeSidebar();
  };

  const openConv = async (cid: number, project: string | null) => {
    setWelcome(false);
    setSettingsOpen(false);     // clicking a conversation always leaves settings/welcome
    setActiveCid(cid);
    const msgs = await getMessages(cid);
    const hasMsgs = msgs.length > 0;
    setScopeLocked(!!project || hasMsgs);
    setActiveProject(project || null);
    setMessages(msgs);
    refreshConvs();
    closeSidebar();
  };

  const newChat = async () => {
    setWelcome(false);
    setSettingsOpen(false);
    setLogoWink(true);
    setTimeout(() => setLogoWink(false), 1200);   // logo winks, then back to normal
    const cid = await newConversation();
    setActiveCid(cid);
    setActiveProject(null);
    setScopeLocked(false);
    setMessages([]);
    setComposerFocus((k) => k + 1);
    refreshConvs();
    closeSidebar();
  };

  const deleteConv = async (cid: number) => {
    const ok = await askConfirm("this is a waste of tokens mate!!", "Delete now!!");
    if (!ok) return;
    await deleteConversation(cid);
    const cs = await getConversations();
    setConversations(cs);
    if (cid === activeCid) {
      // Return to the welcome state rather than auto-selecting another chat.
      setActiveCid(null);
      setActiveProject(null);
      setMessages([]);
      setSettingsOpen(false);
      setWelcome(true);
    }
    toast("Conversation deleted");
  };

  const chooseProject = (p: string) => { setActiveProject(p); setComposerFocus((k) => k + 1); };

  const ask = async (question: string) => {
    if (busy) return;
    let cid = activeCid;
    if (cid === null) { cid = await newConversation(); setActiveCid(cid); }
    if (!scopeLocked && !activeProject) { toast("Choose a BRD to ground this chat first"); return; }

    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: question }, { role: "assistant", content: "", loading: true }]);

    let acc = "", started = false;
    let finalData: Extract<import("./types").AskEvent, { done: true }> | null = null;
    try {
      for await (const ev of askStream(question, cid, activeProject)) {
        if ("t" in ev) {
          acc += ev.t; started = true;
          setMessages((m) => updLast(m, { content: acc, loading: false, streaming: true }));
        } else if ("done" in ev) {
          finalData = ev;
        } else if ("error" in ev) {
          setMessages((m) => updLast(m, { content: "⚠ " + ev.error, loading: false, streaming: false, error: true }));
          toast(ev.error);
        }
      }
      if (finalData) {
        const fd = finalData;
        setActiveCid(fd.conversation_id);
        const cond = fd.standalone && fd.standalone.trim() !== question.trim() ? fd.standalone : undefined;
        setMessages((m) => updLast(m, { content: fd.answer, sources: fd.sources, cond, loading: false, streaming: false }));
        setScopeLocked(true);
        refreshConvs();
      } else if (!started) {
        setMessages((m) => updLast(m, { content: "⚠ no response", loading: false, error: true }));
      }
    } catch {
      setMessages((m) => updLast(m, { content: "⚠ request failed", loading: false, streaming: false, error: true }));
      toast("Request failed");
    }
    setBusy(false);
  };

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
      const cs = await getConversations();
      setConversations(cs);
      // Land on the welcome state — don't auto-open a conversation.
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ready = scopeLocked || !!activeProject;

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
        email={user.email}
        onLogout={onLogout}
        onOpenSettings={() => { setSettingsOpen(true); closeSidebar(); }}
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
            <ScopePicker
              projects={projects}
              activeProject={activeProject}
              projTitle={projTitle}
              scopeLocked={scopeLocked}
              onChoose={chooseProject}
            />
          )}
        </div>
        {settingsOpen ? (
          <AccountSettings email={user.email} toast={toast} />
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
              disabled={busy || !ready}
              placeholder={ready ? "Ask about the requirements…" : "Select a BRD to start chatting…"}
              onSend={ask}
              focusKey={composerFocus}
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
      <ConfirmModal state={confirm} />
      <div className={"toast" + (toastMsg ? " show" : "")}>{toastMsg?.msg}</div>
    </>
  );
}
