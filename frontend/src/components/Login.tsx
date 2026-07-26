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
import { api, ApiError, type AuthContext, type PlexPoll } from "../api";
import { trapTab } from "./ModalShell";
import { BrandBadge } from "../brand/BrandBadge";
import { ServerPickList, usePlexPinPoll } from "./PlexPin";

function PlexGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">
      <path d="M4 2h6l6 10-6 10H4l6-10L4 2z" />
    </svg>
  );
}

/** The Plex sign-in button and its wait-for-approval state.
 *
 *  We ask the backend for a PIN, open the plex.tv auth page in a popup, and then
 *  poll the backend until the user approves it there. The browser never sees a
 *  Plex token -- the backend polls plex.tv and does the ownership check; we only
 *  learn "ok" or "why not". The polling itself is the shared flow in PlexPin.tsx,
 *  which the Settings link panel drives too. */
function PlexButton({ setup, onAuthed }: { setup: boolean; onAuthed: () => void }) {
  const [phase, setPhase] = useState<"idle" | "waiting" | "choose" | "error">("idle");
  const [error, setError] = useState("");
  const [authUrl, setAuthUrl] = useState("");

  const pin = usePlexPinPoll<PlexPoll>({
    poll: (pinId, machineId) => api.plexPoll(pinId, machineId),
    onOk: () => onAuthed(),
    // On first-run setup, an account owning several servers answers "choose_server": the
    // sign-in stays valid while the picker is up, and the pick carries the answer.
    onChooseServer: () => setPhase("choose"),
    onTimedOut: () => {
      setPhase("error");
      setError("Plex sign-in timed out. Please try again.");
    },
    onFailed: (failure) => {
      setPhase("error");
      setError(failure);
    },
  });

  const start = async () => {
    setPhase("waiting");
    setError("");
    try {
      const { pin_id, auth_url } = await api.plexStart();
      setAuthUrl(auth_url);
      // noopener, matching PlexPanel.startLink. Without it plex.tv gets a `window.opener`
      // handle to the window this page is running in and can navigate it -- to a look-alike
      // of the very page that takes the operator's Reaper password (S-4). The window is
      // sized here only as a hint; noopener is what matters.
      //
      // We used to hold the returned handle and close the popup ourselves once the sign-in
      // landed. noopener makes window.open return null, so that is gone, deliberately: the
      // handle we held and the handle plex.tv held were the same relationship, and the
      // browser grants close() *because* of it. The Plex window stays up until the operator
      // closes it, which is already what the Settings link flow does.
      window.open(auth_url, "_blank", "width=620,height=760,noopener");
      pin.begin(pin_id);
    } catch (e) {
      setPhase("error");
      setError(e instanceof ApiError ? e.message : "Could not reach Plex to start sign-in.");
    }
  };

  const cancel = () => {
    pin.cancel();
    setPhase("idle");
  };

  if (phase === "waiting") {
    return (
      <div className="plex-waiting">
        <span className="spinner" aria-hidden="true" />
        <div>
          <strong>Waiting for Plex…</strong>
          {/* Once the sign-in is approved, the wait can continue for a second reason: the
              server itself isn't answering yet. Say which one it is, so a longer wait
              doesn't read as a hang. Reaper keeps polling either way; the sign-in stays
              good. Worded the same as the Settings panel's copy. */}
          <p className="muted">
            {pin.retrying ?? (
              <>
                Approve the sign-in in the Plex window.{" "}
                <a href={authUrl} target="_blank" rel="noreferrer">
                  Didn’t open?
                </a>
              </>
            )}
          </p>
        </div>
        <button className="link" onClick={cancel}>
          Cancel
        </button>
      </div>
    );
  }

  if (phase === "choose") {
    return (
      <div className="server-pick">
        <strong>Which server should Reaper manage?</strong>
        <p className="muted">
          This account owns more than one Plex server. Reaper will only ever scan and prune the one
          you pick. You can change it later in Settings.
        </p>
        <ServerPickList
          servers={pin.servers ?? []}
          onPick={(machineId) => {
            setPhase("waiting");
            void pin.pick(machineId);
          }}
          onCancel={cancel}
        />
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

/** The local-account form, in a sheet that slides up from the bottom.
 *
 *  This is the one dialog that does not go through ModalShell: it stays mounted and slides,
 *  rather than appearing over a scrim. It borrows ModalShell's `trapTab` rather than
 *  hand-rolling a second one, so the sheet keeps the promise its `aria-modal="true"` makes:
 *  while it is open, Tab cannot reach the page behind it. */
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
  const usernameRef = useRef<HTMLInputElement | null>(null);
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const invoker = useRef<HTMLElement | null>(null);

  // Close on Escape while the sheet is open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // The sheet stays mounted and slides in, so opening it has to move focus by hand:
  // autoFocus only fires at mount, and at mount this is closed. Closing hands focus back
  // to whatever opened it, so the keyboard does not land back at the top of the page.
  useEffect(() => {
    if (open) {
      invoker.current = document.activeElement as HTMLElement | null;
      // With no local account yet the sheet has no form, so focus falls to the sheet
      // itself: it must never stay on the page the sheet has just covered.
      if (usernameRef.current) usernameRef.current.focus();
      else sheetRef.current?.focus();
    } else {
      invoker.current?.focus();
      invoker.current = null;
    }
  }, [open]);

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
      {/* Closed, the sheet is only translated off-screen, so without `inert` its fields
          and buttons stay in the tab order invisibly and it announces itself as an open
          dialog. `inert` takes them all out at once and leaves the slide animation
          untouched. */}
      <div
        ref={sheetRef}
        className={open ? "sheet open" : "sheet"}
        inert={!open}
        role="dialog"
        aria-modal="true"
        aria-label="Local account sign-in"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => trapTab(e, open ? sheetRef.current : null)}
      >
        <div className="sheet-grip" aria-hidden="true" />
        <h2>Local account</h2>
        <p className="muted sheet-blurb">
          The fallback for when Plex is unreachable. Reaper always keeps at least one local account
          so a plex.tv outage can’t lock you out.
        </p>

        {noLocalYet ? (
          <p className="notice notice-warn">
            No local account exists yet. Create one on the host with{" "}
            <code>reaper-admin create-admin --username &lt;name&gt;</code>, or sign in with Plex
            above.
          </p>
        ) : (
          <form onSubmit={submit} className="local-form">
            <label className="field">
              <span className="field-label">Username</span>
              {/* The lengths the server accepts, so a long pasted passphrase is stopped in
                  the box rather than coming back as a validator's sentence. */}
              <input
                ref={usernameRef}
                autoComplete="username"
                maxLength={128}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">Password</span>
              <input
                type="password"
                autoComplete="current-password"
                maxLength={128}
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
      <BrandBadge className="brand-badge" />
      <h1 className="brand-word">Recovery</h1>
      <p className="auth-tagline">Single-use admin access</p>
      {/* The console, NOT "its log": mint_recovery_token prints the banner deliberately
          (auth/recovery.py), so the code never reaches structlog, the in-app Logs tab, or
          the log files that tab downloads. Sending a locked-out operator to Settings ->
          Logs to find it left them concluding recovery was broken (U-11). */}
      <p className="auth-note">
        Reaper printed a recovery code to the container's console output. Paste it here to sign in
        as an admin so you can reset a password or re-link Plex; the code expires the moment it is
        used.
      </p>
      <form onSubmit={submit} className="local-form">
        <label className="field">
          <span className="field-label">Recovery code</span>
          <input
            autoFocus
            autoComplete="off"
            spellCheck={false}
            maxLength={256}
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
          <BrandBadge className="brand-badge" />
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
            Reaper can permanently delete media. Only the server’s owner is admitted. Authenticating
            with Plex is not enough.
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
