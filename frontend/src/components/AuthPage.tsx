import { useState } from "react";
import {
  forgotPassword, isOtpStage, login, resendOtp, resetPassword, signup, verifyOtp,
  type AuthUser, type OtpStage,
} from "../lib/api";
import { LOGO_SVG } from "../lib/constants";

type Mode = "login" | "signup" | "forgot";

export default function AuthPage({ onAuthed, initialNotice }: { onAuthed: (u: AuthUser) => void; initialNotice?: string | null }) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [notice] = useState<string | null>(initialNotice ?? null);
  const [busy, setBusy] = useState(false);

  // OTP (2-step) state: set once a signup/login/forgot returns a code challenge.
  const [otp, setOtp] = useState<OtpStage | null>(null);
  const [code, setCode] = useState("");
  const [newPw, setNewPw] = useState("");   // only used for a reset challenge
  const [resent, setResent] = useState(false);

  const swap = (m: Mode) => { setMode(m); setErr(null); setOtp(null); setCode(""); setNewPw(""); };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      if (mode === "forgot") {
        setOtp(await forgotPassword(email));          // -> reset code challenge
      } else {
        const res = mode === "login" ? await login(email, pw) : await signup(email, pw);
        if (isOtpStage(res)) setOtp(res);             // signup verify or login step-up
        else onAuthed(res);
      }
    } catch (ex) {
      setErr((ex as Error).message);
    }
    setBusy(false);
  };

  const submitCode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!otp) return;
    setErr(null);
    setBusy(true);
    try {
      const u = otp.kind === "reset"
        ? await resetPassword(otp.email, code.trim(), newPw)
        : await verifyOtp(otp.email, code.trim(), otp.kind);
      onAuthed(u);
    } catch (ex) {
      setErr((ex as Error).message);
    }
    setBusy(false);
  };

  const resend = async () => {
    if (!otp) return;
    await resendOtp(otp.email, otp.kind);
    setResent(true);
  };

  const title = otp
    ? (otp.kind === "reset" ? "Reset your password" : otp.kind === "login" ? "Verify it's you" : "Verify your email")
    : mode === "login" ? "Welcome back" : mode === "signup" ? "Create your account" : "Reset your password";

  return (
    <div className="authwrap">
      <form className="authcard" onSubmit={otp ? submitCode : submit}>
        <div className="authbrand">
          <span className="logo" dangerouslySetInnerHTML={{ __html: LOGO_SVG }} />
          <div className="wm"><b>Retrieval</b><small>BRD ENGINE</small></div>
        </div>
        <h1>{title}</h1>
        <p className="authsub">Ask your Business Requirements Documents, get grounded, cited answers.</p>

        {notice && !otp && <div className="authnotice">{notice}</div>}

        {otp ? (
          <>
            <div className="authnotice">We sent a 6-digit code to <b>{otp.email}</b>. Enter it below.</div>
            <label className="authfld">
              <span>Verification code</span>
              <input value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                     inputMode="numeric" autoComplete="one-time-code" placeholder="123456" required autoFocus />
            </label>
            {otp.kind === "reset" && (
              <label className="authfld">
                <span>New password</span>
                <input type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)}
                       autoComplete="new-password" placeholder="at least 8 characters" required />
              </label>
            )}

            {err && <div className="autherr">{err}</div>}

            <button className="primary" type="submit" disabled={busy || code.length < 6}>
              {busy ? "…" : otp.kind === "reset" ? "Set new password" : "Verify"}
            </button>

            <div className="authswitch">
              <button type="button" onClick={resend} disabled={resent}>{resent ? "Code sent ✓" : "Resend code"}</button>
              <span className="authdot">·</span>
              <button type="button" onClick={() => swap("login")}>Back to log in</button>
            </div>
          </>
        ) : (
          <>
            <label className="authfld">
              <span>Email</span>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" placeholder="you@company.com" required />
            </label>
            {mode !== "forgot" && (
              <label className="authfld">
                <span>Password</span>
                <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} placeholder={mode === "signup" ? "at least 8 characters" : "••••••••"} required />
              </label>
            )}

            {err && <div className="autherr">{err}</div>}

            <button className="primary" type="submit" disabled={busy}>
              {busy ? "…" : mode === "login" ? "Log in" : mode === "signup" ? "Sign up" : "Send code"}
            </button>

            {mode === "login" && (
              <div className="authswitch">
                <button type="button" onClick={() => swap("forgot")}>Forgot password?</button>
                <span className="authdot">·</span>
                New here? <button type="button" onClick={() => swap("signup")}>Create an account</button>
              </div>
            )}
            {mode === "signup" && (
              <div className="authswitch">Already have an account? <button type="button" onClick={() => swap("login")}>Log in</button></div>
            )}
            {mode === "forgot" && (
              <div className="authswitch"><button type="button" onClick={() => swap("login")}>Back to log in</button></div>
            )}
          </>
        )}
      </form>
    </div>
  );
}
