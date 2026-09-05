import type { ConversationMeta } from "../types";
import { LOGO_SVG, LOGO_WINK_SVG, MOON_SVG, SUN_SVG } from "../lib/constants";

const MANAGE_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/></svg>';
const TRASH_SVG =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';

export default function Sidebar({
  open,
  theme,
  conversations,
  activeCid,
  projLabel,
  onHome,
  logoWink,
  onNewChat,
  onManage,
  onToggleTheme,
  onOpenConv,
  onDeleteConv,
  email,
  onLogout,
  onOpenSettings,
}: {
  open: boolean;
  theme: string;
  conversations: ConversationMeta[];
  activeCid: number | null;
  projLabel: (p: string) => string;
  onHome: () => void;
  logoWink: boolean;
  onNewChat: () => void;
  onManage: () => void;
  onToggleTheme: () => void;
  onOpenConv: (cid: number, project: string | null) => void;
  onDeleteConv: (cid: number) => void;
  email: string;
  onLogout: () => void;
  onOpenSettings: () => void;
}) {
  return (
    <aside className={open ? "open" : ""}>
      <div className="side-top">
        <div className="brand">
          <button className="brand-home" onClick={onHome} title="Home" aria-label="Go to home">
            <span className={"logo" + (logoWink ? " is-wink" : "")}>
              <span className="logo-open" dangerouslySetInnerHTML={{ __html: LOGO_SVG }} />
              <span className="logo-wink" dangerouslySetInnerHTML={{ __html: LOGO_WINK_SVG }} />
            </span>
            <span className="wm"><b>Retrieval</b><small>BRD ENGINE</small></span>
          </button>
          <button
            className="themebtn"
            aria-label="Toggle light or dark theme"
            title="Toggle theme"
            onClick={onToggleTheme}
            dangerouslySetInnerHTML={{ __html: theme === "light" ? MOON_SVG : SUN_SVG }}
          />
        </div>
        <button className="newbtn" onClick={onNewChat}>
          <span className="plus" aria-hidden="true">+</span> New chat <kbd className="k">N</kbd>
        </button>
        <button className="uploadbtn" title="Add or delete BRDs" onClick={onManage}>
          <span className="upl" aria-hidden="true" dangerouslySetInnerHTML={{ __html: MANAGE_SVG }} />
          <span className="upl-label">Manage BRDs</span>
        </button>
      </div>
      <div className="convs-h">Conversations</div>
      <div className="convs">
        {conversations.map((c) => (
          <div
            key={c.id}
            className={"conv" + (c.id === activeCid ? " active" : "")}
            onClick={() => onOpenConv(c.id, c.project)}
          >
            <div className="title">{c.preview && c.preview.trim() ? c.preview : "New conversation"}</div>
            <span className="when">{new Date(c.created_at).toLocaleString()}</span>
            {c.project && <span className="convtag">{projLabel(c.project)}</span>}
            <button
              className="del"
              title="Delete conversation"
              aria-label="Delete conversation"
              onClick={(e) => { e.stopPropagation(); onDeleteConv(c.id); }}
              dangerouslySetInnerHTML={{ __html: TRASH_SVG }}
            />
          </div>
        ))}
      </div>
      <div className="side-foot">
        <span className="dotlive"></span>
        <button className="side-email" title="Account settings" onClick={onOpenSettings}>{email}</button>
        <button className="logoutbtn" title="Log out" aria-label="Log out" onClick={onLogout}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
            <polyline points="16 17 21 12 16 7" />
            <line x1="21" y1="12" x2="9" y2="12" />
          </svg>
        </button>
      </div>
    </aside>
  );
}
