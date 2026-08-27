// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one Plex PIN sign-in flow, shared by the login screen and the Settings link panel.
//
// Both places do the same dance: ask the backend for a PIN, send the operator to plex.tv
// to approve it, then poll the backend until it answers "ok", "this account owns several
// servers, pick one", or nothing at all. Only the endpoint and what happens afterwards
// differ, so the state machine (deadline, timer, the pick, the cancel) lives here once,
// and the two callers pass their own poll function and outcome handlers.

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import type { PlexResourceConnection, PlexServerChoice, ReasonKey } from "../api";
import { describeError } from "../errors";
import i18next from "../i18n";
import { composeError } from "../why";

/** The sentinel the connection select uses for "let me type one". Not a URI, so it can
 *  never collide with a discovered address. Shared by `PlexPanel` and `SetupPlexStep` so the
 *  two never drift onto different values for the same meaning. */
export const MANUAL_CONNECTION = "__manual__";

/** The label a connection shows in the picker: where it goes, then how.
 *
 *  plex.direct hostnames embed the address as dashes ("192-168-20-73.abc….plex.direct"),
 *  which reads as noise. This shows the plain address instead, and reports the certificate
 *  separately as the "secure" tag. The full URI stays the option's value, so what is saved
 *  is exact.
 *
 *  Shared by `PlexPanel` and `SetupPlexStep` so an address reads the same in both places,
 *  through the same `plex.connectionKind.*` / `plex.connectionLabel.*` catalog keys. */
export function connectionLabel(c: PlexResourceConnection): string {
  const kind = c.relay
    ? i18next.t("plex.connectionKind.relay")
    : c.local
      ? i18next.t("plex.connectionKind.local")
      : i18next.t("plex.connectionKind.remote");
  let host = c.uri.replace(/^https?:\/\//, "");
  const direct = /^(\d+)-(\d+)-(\d+)-(\d+)\.[0-9a-f]+\.plex\.direct(:\d+)?$/i.exec(host);
  if (direct) host = `${direct[1]}.${direct[2]}.${direct[3]}.${direct[4]}${direct[5] ?? ""}`;
  return c.protocol === "https"
    ? i18next.t("plex.connectionLabel.secure", { kind, host })
    : i18next.t("plex.connectionLabel.plain", { kind, host });
}

/** What the app says when a sign-in lands on the server picker.
 *
 *  The login screen and the Settings link panel both reach this state through this hook. Say
 *  this sentence only here, never again in either caller: `PlexPin.test.tsx` reads both
 *  callers' source and fails by name if either one states it again.
 *
 *  A function, not a constant: this module is in the eager bundle, so a string resolved in its
 *  body would stay English for the life of the page (`i18n-module-scope.test.ts`). */
export const chooseServerSaid = () => i18next.t("plex.pin.chooseServerSaid");

/** Poll every two seconds. The wait is a person staring at a browser tab, so this favors
 *  responsiveness: two seconds costs at most 150 requests to our own backend over the
 *  five-minute deadline. */
const POLL_MS = 2000;

/** Give the wait a deadline. Without one, an operator who opens the approval page and
 *  never approves leaves the browser polling forever, with the button stuck disabled
 *  until a full page reload. */
const DEADLINE_MS = 5 * 60 * 1000;

/** What both poll endpoints answer with. */
export interface PinPollResult {
  /** `retrying` is a non-final status, like `pending`: the sign-in was approved but the Plex
   *  server did not answer this instant (it may be restarting). The backend answers this
   *  instead of throwing, because a thrown status would stop this loop for good and send the
   *  operator back through the whole approval round trip for a sign-in that is still good. */
  status: "pending" | "retrying" | "ok" | "choose_server";
  servers: PlexServerChoice[] | null;
  /** Present only with status "retrying": why this poll couldn't finish yet, composed
   *  through `why.ts`'s `composeError`. */
  reason?: ReasonKey | null;
}

interface PinPollHandlers<R extends PinPollResult> {
  /** Ask the backend where the PIN stands. */
  poll: (pinId: number, machineId?: string) => Promise<R>;
  /** The sign-in finished. */
  onOk: (result: R) => void;
  /** The account owns several servers; the list is held until one is picked. */
  onChooseServer?: (servers: PlexServerChoice[]) => void;
  /** Nobody approved it in time. */
  onTimedOut: () => void;
  /** The poll itself failed. The message is already operator-readable. */
  onFailed: (message: string) => void;
}

function failureText(e: unknown): string {
  return e instanceof Error ? describeError(e) : i18next.t("plex.pin.signInFailedFallback");
}

/** Drive one PIN through to a final answer.
 *
 *  `begin` starts polling for a PIN that has been opened for approval, `pick` carries the
 *  operator's server choice, and `cancel` stops everything. Any final outcome (ok, timed
 *  out, failed) stops the timer and drops the server list before the handler runs. */
export function usePlexPinPoll<R extends PinPollResult>(handlers: PinPollHandlers<R>) {
  const [servers, setServers] = useState<PlexServerChoice[] | null>(null);
  // Why a still-running wait is taking longer than usual, straight from the backend.
  // Cleared as soon as a poll gets past it, so a blip that resolves leaves no trace.
  const [retrying, setRetrying] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const pinRef = useRef<number | null>(null);
  // The last reason spoken, so a poll repeating it every two seconds is said once. Cleared by
  // `stop`, which every new wait runs first, so the next sign-in hears its reason again.
  const saidRef = useRef<string | null>(null);

  // Stopping the timer does not stop a request already in the air, so every poll carries the
  // run it belongs to, and a finished run ignores whatever lands late. Without this, two slow
  // polls could overlap: the second answers "ok" and signs the operator in, then the first
  // rejects (the PIN is now consumed, or plex.tv rate-limited the request) and paints "Plex
  // sign-in failed" over a session that actually succeeded. The mirror failure through `cancel`
  // would sign the operator in after they pressed Cancel. `begin`, `stop`, and `cancel` all bump
  // the run.
  const runRef = useRef(0);
  // One request at a time. A poll slower than the two-second tick would otherwise stack up
  // against plex.tv, and the pile is what makes the overlap above likely in the first place.
  const inFlightRef = useRef(false);

  // The running timer reads the handlers through a ref, so a re-render never restarts the
  // poll and the callback never calls a stale closure.
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  const stop = useCallback(() => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = null;
    runRef.current += 1;
    inFlightRef.current = false;
    saidRef.current = null;
  }, []);

  useEffect(() => stop, [stop]);

  const begin = useCallback(
    (pinId: number, machineId?: string) => {
      stop();
      pinRef.current = pinId;
      const run = runRef.current;
      const deadline = Date.now() + DEADLINE_MS;
      timerRef.current = window.setInterval(() => {
        const h = handlersRef.current;
        if (Date.now() > deadline) {
          stop();
          setServers(null);
          setRetrying(null);
          h.onTimedOut();
          return;
        }
        if (inFlightRef.current) return;
        inFlightRef.current = true;
        void (async () => {
          try {
            const result = await h.poll(pinId, machineId);
            // Everything below settles the sign-in, so nothing may run for a run that has
            // already ended: `stop` has bumped the counter, and this answer is about a PIN
            // nobody is waiting on any more.
            if (run !== runRef.current) return;
            if (result.status === "ok") {
              stop();
              setServers(null);
              setRetrying(null);
              h.onOk(result);
            } else if (result.status === "choose_server") {
              // The sign-in stays valid while the picker is up. Stop polling and hold the list
              // until the operator picks one.
              stop();
              setRetrying(null);
              setServers(result.servers ?? []);
              // Announces here, when a poll's answer moves to "choose_server", because this is
              // the one copy both sign-in paths share, so a third caller inherits the
              // announcement instead of forgetting it. Told and not focused: the picker
              // replaces the wait on a timer, so moving focus here would steal it rather than
              // recover it.
              announce(chooseServerSaid());
              h.onChooseServer?.(result.servers ?? []);
            } else {
              // "pending" or "retrying": neither is final, so keep polling. Only "retrying"
              // carries a reason. Say it, so a longer-than-usual wait is explained rather than
              // looking like a hang.
              const reason =
                result.status === "retrying" && result.reason ? composeError(result.reason) : null;
              setRetrying(reason);
              // Also says it out loud, for two reasons. Every transition in this flow is driven
              // by the two-second poll rather than by the operator, so this paragraph can change
              // with nobody touching anything: it changes on screen, but a screen reader would
              // not otherwise notice. And this hook is the one copy both sign-in paths share, so
              // announcing it here means a third caller inherits the announcement instead of
              // forgetting it. The paragraph this replaces holds a link, so announcing just the
              // reason text avoids turning that whole paragraph into a live region with
              // interactive content inside it.
              //
              // Announces only when the reason text actually changes: the poll repeats the same
              // reason every two seconds, and re-announcing it unchanged would talk over
              // everything else on the page.
              if (reason !== null && reason !== saidRef.current) announce(reason);
              saidRef.current = reason;
            }
          } catch (e) {
            if (run !== runRef.current) return;
            stop();
            setServers(null);
            setRetrying(null);
            h.onFailed(failureText(e));
          } finally {
            if (run === runRef.current) inFlightRef.current = false;
          }
        })();
      }, POLL_MS);
    },
    [stop],
  );

  /** The operator picked a server. One immediate poll usually finishes the job. A "pending"
   *  answer, plex.tv asking us to slow down, falls back to polling. */
  const pick = useCallback(
    async (machineId: string) => {
      const pinId = pinRef.current;
      if (pinId == null) return;
      setServers(null);
      setRetrying(null);
      const h = handlersRef.current;
      // The pick is a poll like any other, so it answers to the same run guard: Cancel while
      // plex.tv is thinking must not sign the operator in a moment later.
      const run = runRef.current;
      try {
        const result = await h.poll(pinId, machineId);
        if (run !== runRef.current) return;
        if (result.status === "ok") {
          h.onOk(result);
        } else if (result.status === "choose_server") {
          setServers(result.servers ?? []);
          // Announces here too: the pick can land back on the picker, when the account's
          // server list changed while it was being read, and that arrives the same way as the
          // first time, on an answer rather than on a press.
          announce(chooseServerSaid());
          h.onChooseServer?.(result.servers ?? []);
        } else {
          begin(pinId, machineId);
        }
      } catch (e) {
        if (run !== runRef.current) return;
        h.onFailed(failureText(e));
      }
    },
    [begin],
  );

  const cancel = useCallback(() => {
    stop();
    setServers(null);
    setRetrying(null);
  }, [stop]);

  return { servers, retrying, begin, pick, cancel };
}

/** One tappable row per server the account owns, and a way out. The surrounding
 *  explanation belongs to the caller: the login screen and Settings word it for their own
 *  context, but the list itself is the same list. */
export function ServerPickList({
  servers,
  onPick,
  onCancel,
}: {
  servers: PlexServerChoice[];
  onPick: (machineId: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      {servers.map((s) => (
        <button
          key={s.machine_identifier}
          type="button"
          className="server-pick-row"
          onClick={() => onPick(s.machine_identifier)}
        >
          {s.name}
        </button>
      ))}
      <button type="button" className="link" onClick={onCancel}>
        {t("common.cancel")}
      </button>
    </>
  );
}
