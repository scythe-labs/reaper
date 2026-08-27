// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The front door. Nothing else in the app renders until someone gets through it.
//
// Two ways in, weighted deliberately. Plex is the prominent path. It is how the
// owner proves they own *this* server, which is the whole authorization story, so
// it is the big button. Local login is the fallback for when plex.tv is down or
// you are locked out, so it lives in a sheet that slides up from the bottom only
// when asked for. On a fresh install the same Plex button does setup: the first
// owner to sign in claims the server.

import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";
import { api, ApiError, type AuthContext, type PlexPoll } from "../api";
import { describeError } from "../errors";
import { trapTab } from "./ModalShell";
import { BrandBadge } from "../brand/BrandBadge";
import { ServerPickList, usePlexPinPoll } from "./PlexPin";
import { Notice } from "./Notice";

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
 *  Plex token. The backend polls plex.tv and does the ownership check, and we
 *  only learn "ok" or "why not". The polling itself is the shared flow in
 *  PlexPin.tsx, which the Settings link panel drives too. */
function PlexButton({ setup, onAuthed }: { setup: boolean; onAuthed: () => void }) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<"idle" | "waiting" | "choose" | "error">("idle");
  const [error, setError] = useState("");
  const [authUrl, setAuthUrl] = useState("");

  const pin = usePlexPinPoll<PlexPoll>({
    poll: (pinId, machineId) => api.plexPoll(pinId, machineId),
    onOk: () => onAuthed(),
    // On first-run setup, an account owning several servers answers "choose_server": the
    // sign-in stays valid while the picker is up, and the pick carries the answer.
    //
    // The announcement that goes with it is `usePlexPinPoll`'s (`chooseServerSaid`), not this
    // handler's. Hand-writing it into both callers risks the two drifting out of step, one
    // announcing the picker while the other says nothing. The hook speaks for every caller
    // instead, so this one only has to move itself.
    onChooseServer: () => setPhase("choose"),
    onTimedOut: () => {
      setPhase("error");
      setError(t("plex.signInTimedOut"));
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
      // noopener, matching PlexPanel.startLink. Without it, plex.tv gets a `window.opener`
      // handle to the window this page is running in and can navigate it to a look-alike of
      // the very page that takes the operator's Reaper password. The window is sized here
      // only as a hint. noopener is what matters.
      //
      // noopener also means window.open returns null, so this page holds nothing to close
      // the window with. It does not need to: `auth_url` carries a forwardUrl back to
      // Reaper's own /plex-done.html, and that page closes the window it is loaded in. A
      // script-opened window can close itself with no opener, so using noopener here costs
      // nothing: the two are independent, not a trade-off.
      window.open(auth_url, "_blank", "width=620,height=760,noopener");
      pin.begin(pin_id);
    } catch (e) {
      setPhase("error");
      setError(
        e instanceof ApiError ? describeError(e) : t("login.plexButton.startFailedFallback"),
      );
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
          <strong>{t("plex.waitingForPlex")}</strong>
          {/* Once the sign-in is approved, the wait can continue for a second reason: the
              server itself isn't answering yet. Say which one it is, so a longer wait
              doesn't read as a hang. Reaper keeps polling either way, and the sign-in stays
              good. This is worded the same as the Settings panel's copy, literally the same
              catalog key, so a reword of one is a reword of both. */}
          <p className="muted">
            {pin.retrying ?? (
              <Trans
                i18nKey="plex.waiting.approveWithLink"
                components={{ btn: <a href={authUrl} target="_blank" rel="noreferrer" /> }}
              />
            )}
          </p>
        </div>
        <button className="link" onClick={cancel}>
          {t("common.cancel")}
        </button>
      </div>
    );
  }

  if (phase === "choose") {
    return (
      <div className="server-pick">
        <strong>{t("plex.chooseServer.label")}</strong>
        <p className="muted">{t("login.plexButton.chooseServerHelp")}</p>
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
        {setup ? t("login.plexButton.signInSetup") : t("login.plexButton.signIn")}
      </button>
      {phase === "error" && <Notice tone="error">{error}</Notice>}
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
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const usernameRef = useRef<HTMLInputElement | null>(null);
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const invoker = useRef<HTMLElement | null>(null);

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
      setError(
        err instanceof ApiError ? describeError(err) : t("login.localSheet.signInFailedFallback"),
      );
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
        aria-label={t("login.localSheet.ariaLabel")}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => trapTab(e, open ? sheetRef.current : null)}
      >
        <div className="sheet-grip" aria-hidden="true" />
        <h2>{t("login.localSheet.heading")}</h2>
        {/* The guarantee is only claimed where it holds, never stated flat above a notice
            that could contradict it on a fresh Plex-only install. What makes it true is the
            wizard's mandatory password step, whose `set_password` creates a local admin
            where there is none. `auth.admins.LastAdminError` is what then keeps it true. This
            has three branches, not two: while the context is still loading, Reaper does not
            yet know which install this is, so it says only what the account is for. These are
            three whole sentences in the catalog, not one stem with a bolted-on tail, because
            each is a complete claim a translator can sentence on its own. */}
        <p className="muted sheet-blurb">
          {!ctx
            ? t("login.localSheet.blurbLoading")
            : noLocalYet
              ? t("login.localSheet.blurbNoLocalYet")
              : t("login.localSheet.blurbHasLocal")}
        </p>

        {/* `standing`: a property of the install, read from `["authContext"]` on first load, so
            it is on this sheet before anything is pressed. The sheet is always mounted and only
            translated off-screen, so the "Use a local account" press reveals a node that was
            already there rather than causing this. */}
        {noLocalYet ? (
          <Notice tone="warn" standing>
            {/* A name that can be pasted, not a placeholder to fill in. `admin` is what the
                wizard's own password step would have created, and any local account arms
                deletion and confirms a restore just the same (`admin_password.verify` checks
                every local admin, not one by name), so the choice is free and a free choice
                is one the operator should not have to stop and make.

                "Docker and snap installs" is not hedging. The Windows and macOS bundles are
                one PyInstaller executable built from `packaging/pyinstaller/entry.py`, which
                runs the launcher and nothing else, so `reaper-admin` is not on those machines
                at all, and an unqualified sentence would send half the operators after a
                command they do not have. Its siblings carry the same qualification
                (`main.py`'s boot warning and the 409 from `api/auth.py` when a recovery code
                finds no admin). Two deliberately do not: `cli.py`'s lockout warning is
                printed by `reaper-admin` itself, so its reader demonstrably has it, and the
                manual's first-run callout already says "On a Docker install". */}
            <Trans i18nKey="login.localSheet.noLocalYetNotice" components={{ code: <code /> }} />
          </Notice>
        ) : (
          <form onSubmit={submit} className="local-form">
            <label className="field">
              <span className="field-label">{t("login.localSheet.usernameLabel")}</span>
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
              <span className="field-label">{t("login.localSheet.passwordLabel")}</span>
              <input
                type="password"
                autoComplete="current-password"
                maxLength={128}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && <Notice tone="error">{error}</Notice>}
            <div className="sheet-actions">
              <button type="button" className="ghost" onClick={onClose}>
                {t("common.back")}
              </button>
              <button type="submit" className="primary" disabled={busy || !username || !password}>
                {busy ? t("login.localSheet.signingIn") : t("login.localSheet.signIn")}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** The recovery redemption card, shown at /recover. Under REAPER_RECOVERY=true Reaper mints a
 *  single-use CODE and puts it in two places, the console and `recovery.txt` in the data folder;
 *  the operator pastes it here. The code is never carried in the URL, so it stays out of
 *  reverse-proxy access logs. */
function RecoveryCard({ onAuthed }: { onAuthed: () => void }) {
  const { t } = useTranslation();
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
      setError(err instanceof ApiError ? describeError(err) : t("login.recovery.failedFallback"));
    } finally {
      setBusy(false);
    }
  };

  return (
    // `main`, not a div: the sign-in screen is a whole page and had no landmark on it at all,
    // so a screen reader user navigating by landmarks had nothing to jump to. Its twin below
    // is the same fix. Every rule for these two is written against the class, so this changes
    // the accessibility tree and nothing on screen.
    <main className="auth-card">
      <BrandBadge className="brand-badge" />
      <h1 className="brand-word">{t("login.recovery.heading")}</h1>
      <p className="auth-tagline">{t("login.recovery.tagline")}</p>
      {/* The console, not "its log": mint_recovery_token prints the banner deliberately
          (auth/recovery.py), so the code never reaches structlog, the in-app Logs tab, or
          the log files that tab downloads. Sending a locked-out operator to Settings ->
          Logs to find it would leave them concluding recovery was broken.

          The file is named first because it is the only one of the two that exists on every
          install: a windowed Windows build and a Finder-launched .app have no console for
          the banner to reach. The claim "without the old password" is true because of
          `api/settings.py`'s admin-password route. Its siblings are `auth/recovery.py`'s
          banner and the locked-out page in the manual. */}
      <p className="auth-note">
        <Trans i18nKey="login.recovery.note" components={{ code: <code /> }} />
      </p>
      <form onSubmit={submit} className="local-form">
        <label className="field">
          <span className="field-label">{t("login.recovery.codeLabel")}</span>
          <input
            autoFocus
            autoComplete="off"
            spellCheck={false}
            maxLength={256}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={t("login.recovery.codePlaceholder")}
          />
        </label>
        {error && <Notice tone="error">{error}</Notice>}
        <button type="submit" className="primary btn-block" disabled={busy || !code.trim()}>
          {busy ? t("login.recovery.redeeming") : t("login.recovery.redeemButton")}
        </button>
      </form>
    </main>
  );
}

export function Login() {
  const { t } = useTranslation();
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
        <main className="auth-card">
          <BrandBadge className="brand-badge" />
          <h1 className="brand-word">Reaper</h1>
          {/* The one tagline, "Grave decisions, clearly explained": the words the masthead
              shows once you are in (App.tsx), and the ones the README, the manual's site
              header, and the API's own description carry. This card's rendered copy now
              lives in the catalog under `shell.app.brandTagline`, the one key both mastheads
              read. `TAGLINE_SITES` in tests/test_repo_hygiene.py guards this by reading this
              file's raw source for that exact English sentence, quoted above on purpose,
              rather than reading the catalog. */}
          <p className="auth-tagline">{t("shell.app.brandTagline")}</p>
          <p className="auth-note">
            {setup ? t("login.authNoteSetup") : t("login.authNoteSignIn")}
          </p>

          <PlexButton setup={setup} onAuthed={onAuthed} />

          <button className="auth-alt" onClick={() => setSheetOpen(true)}>
            {t("login.useLocalAccount")}
          </button>

          <p className="auth-safety">{t("login.authSafety")}</p>
        </main>
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
