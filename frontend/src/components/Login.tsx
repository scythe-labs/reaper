// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The front door. Nothing else in the app renders until someone gets through it.
//
// Two ways in, weighted deliberately. Plex is the prominent path -- it is how the
// owner proves they own *this* server, which is the whole authorization story --
// so it is the big button. Local login is the fallback for when plex.tv is down or
// you are locked out, so it lives in a sheet that slides up from the bottom only
// when asked for. On a fresh install the same Plex button does setup: the first
// owner to sign in claims the server.

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type AuthContext } from "../api";

function Mark() {
  // A stylized scythe: a handle sweeping down-left, a curved blade across the top.
  return (
    <svg className="brand-mark" viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <path
        d="M31 9C17 9 9 17 9 29c8-8 16-12 26-10-1-5-2-8-4-10Z"
        fill="currentColor"
      />
      <path
        d="M31 9 19 40"
        stroke="currentColor"
        strokeWidth="3.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function PlexGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">
      <path d="M4 2h6l6 10-6 10H4l6-10L4 2z" />
    </svg>
  );
}

/** The Plex sign-in button and its wait-for-approval state machine.
 *
 *  We ask the backend for a PIN, open the plex.tv auth page in a popup, and then
 *  poll the backend until the user approves it there. The browser never sees a
 *  Plex token -- the backend polls plex.tv and does the ownership check; we only
 *  learn "ok" or "why not". */
function PlexButton({ setup, onAuthed }: { setup: boolean; onAuthed: () => void }) {
  const [phase, setPhase] = useState<"idle" | "waiting" | "error">("idle");
  const [error, setError] = useState("");
  const [authUrl, setAuthUrl] = useState("");
  const popup = useRef<Window | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const stop = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = undefined;
  };
  useEffect(() => stop, []);

  const start = async () => {
    setPhase("waiting");
    setError("");
    try {
      const { pin_id, auth_url } = await api.plexStart();
      setAuthUrl(auth_url);
      popup.current = window.open(auth_url, "reaper-plex-auth", "width=620,height=760");

      const deadline = Date.now() + 5 * 60 * 1000;
      timer.current = window.setInterval(async () => {
        if (Date.now() > deadline) {
          stop();
          popup.current?.close();
          setPhase("error");
          setError("Plex sign-in timed out. Please try again.");
          return;
        }
        try {
          const result = await api.plexPoll(pin_id);
          if (result.status === "ok") {
            stop();
            popup.current?.close();
            onAuthed();
          }
        } catch (e) {
          stop();
          popup.current?.close();
          setPhase("error");
          setError(e instanceof ApiError ? e.message : "Plex sign-in failed.");
        }
      }, 2500);
    } catch (e) {
      setPhase("error");
      setError(e instanceof ApiError ? e.message : "Could not reach Plex to start sign-in.");
    }
  };

  const cancel = () => {
    stop();
    popup.current?.close();
    setPhase("idle");
  };

  if (phase === "waiting") {
    return (
      <div className="plex-waiting">
        <span className="spinner" aria-hidden="true" />
        <div>
          <strong>Waiting for Plex…</strong>
          <p className="muted">
            Approve the sign-in in the Plex window.{" "}
            <a href={authUrl} target="_blank" rel="noreferrer">
              Didn’t open?
            </a>
          </p>
        </div>
        <button className="link" onClick={cancel}>
          Cancel
        </button>
      </div>
    );
  }

  return (
    <>
      <button className="btn-plex" onClick={start}>
        <PlexGlyph />
        {setup ? "Sign in with Plex to set up Reaper" : "Sign in with Plex"}
      </button>
      {phase === "error" && <p className="notice notice-error">{error}</p>}
    </>
  );
}

/** The local-account form, in a sheet that slides up from the bottom. */
function LocalSheet({
  open,
  onClose,
  onAuthed,
  ctx,
}: {
  open: boolean;
  onClose: () => void;
  onAuthed: () => void;
  ctx: AuthContext | undefined;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Close on Escape while the sheet is open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.localLogin(username, password);
      onAuthed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  const noLocalYet = ctx && !ctx.local_login_available;

  return (
    <div className={open ? "sheet-scrim open" : "sheet-scrim"} onClick={onClose}>
      <div
        className={open ? "sheet open" : "sheet"}
        role="dialog"
        aria-modal="true"
        aria-label="Local account sign-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sheet-grip" aria-hidden="true" />
        <h2>Local account</h2>
        <p className="muted sheet-blurb">
          The fallback for when Plex is unreachable. Reaper always keeps at least one
          local account so a plex.tv outage can’t lock you out.
        </p>

        {noLocalYet ? (
          <p className="notice notice-warn">
            No local account exists yet. Create one on the host with{" "}
            <code>reaper-admin create-admin --username &lt;name&gt;</code>, or sign in
            with Plex above.
          </p>
        ) : (
          <form onSubmit={submit} className="local-form">
            <label className="field">
              <span className="field-label">Username</span>
              <input
                autoFocus={open}
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">Password</span>
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && <p className="notice notice-error">{error}</p>}
            <div className="sheet-actions">
              <button type="button" className="ghost" onClick={onClose}>
                Back
              </button>
              <button type="submit" className="primary" disabled={busy || !username || !password}>
                {busy ? "Signing in…" : "Sign in"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** The recovery redemption card, shown at /recover. Under REAPER_RECOVERY=true Reaper
 *  prints a single-use CODE to its log; the operator pastes it here. The code is never
 *  carried in the URL, so it stays out of reverse-proxy access logs. */
function RecoveryCard({ onAuthed }: { onAuthed: () => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.recover(code.trim());
      onAuthed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Recovery failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-card">
      <div className="brand-badge">
        <Mark />
      </div>
      <h1 className="brand-word">Recovery</h1>
      <p className="auth-tagline">Single-use admin access</p>
      <p className="auth-note">
        Reaper printed a recovery code to its log. Paste it here to sign in as an admin so
        you can reset a password or re-link Plex; the code expires the moment it is used.
      </p>
      <form onSubmit={submit} className="local-form">
        <label className="field">
          <span className="field-label">Recovery code</span>
          <input
            autoFocus
            autoComplete="off"
            spellCheck={false}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="Paste the code from the log"
          />
        </label>
        {error && <p className="notice notice-error">{error}</p>}
        <button type="submit" className="primary btn-block" disabled={busy || !code.trim()}>
          {busy ? "Redeeming…" : "Redeem recovery code"}
        </button>
      </form>
    </div>
  );
}

export function Login() {
  const queryClient = useQueryClient();
  const { data: ctx } = useQuery({ queryKey: ["authContext"], queryFn: api.authContext });
  const [sheetOpen, setSheetOpen] = useState(false);

  const onAuthed = () => {
    void queryClient.invalidateQueries({ queryKey: ["me"] });
  };

  // Recovery is a path, not a query param: the code is pasted, never carried in the URL.
  const onRecoverPath = window.location.pathname.replace(/\/+$/, "") === "/recover";
  const setup = ctx?.setup_needed ?? false;

  return (
    <div className="auth-screen">
      <div className="auth-aurora" aria-hidden="true" />

      {onRecoverPath ? (
        <RecoveryCard onAuthed={onAuthed} />
      ) : (
        <div className="auth-card">
          <div className="brand-badge">
            <Mark />
          </div>
          <h1 className="brand-word">Reaper</h1>
          <p className="auth-tagline">Explainable pruning for Plex</p>
          <p className="auth-note">
            {setup
              ? "Set up Reaper by signing in as the owner of the Plex server."
              : "Sign in to review, explain, and prune your library."}
          </p>

          <PlexButton setup={setup} onAuthed={onAuthed} />

          <button className="auth-alt" onClick={() => setSheetOpen(true)}>
            Use a local account
          </button>

          <p className="auth-safety">
            Reaper can permanently delete media. Only the server’s owner is admitted.
            Authenticating with Plex is not enough.
          </p>
        </div>
      )}

      <LocalSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        onAuthed={onAuthed}
        ctx={ctx}
      />
    </div>
  );
}
