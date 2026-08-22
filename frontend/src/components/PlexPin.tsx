// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one Plex PIN sign-in flow, shared by the login screen and the Settings link panel.
//
// Both places do the same dance: ask the backend for a PIN, send the operator to plex.tv
// to approve it, then poll the backend until it answers "ok", "this account owns several
// servers, pick one", or nothing at all. Only the endpoint and what happens afterwards
// differ, so the state machine (deadline, timer, the pick, the cancel) lives here once
// and the two callers pass their own poll function and outcome handlers. It used to live
// in both files, and the two copies had already drifted apart.

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import type { PlexServerChoice, ReasonKey } from "../api";
import { describeError } from "../errors";
import i18next from "../i18n";
import { composeError } from "../why";

/** What the app says when a sign-in lands on the server picker.
 *
 *  The login screen and the Settings link panel both reach this state through this hook, and
 *  each said this sentence by hand in its own words (rule 144). They had already been out of
 *  step once: Settings announced the picker and the login screen said nothing (#177, rule 72).
 *
 *  Exported because `PlexPin.test.tsx` reads the two callers' source and fails by name in
 *  whichever one states it again. Resolved from the catalog at module load, which is safe
 *  because `../i18n` inits synchronously with inline resources. */
export const CHOOSE_SERVER_SAID = i18next.t("plex.pin.chooseServerSaid");

/** Poll every two seconds. The wait is a person staring at a browser tab, so the faster
 *  of the two intervals this replaced wins: two seconds still costs at most 150 requests
 *  to our own backend over the five-minute deadline, and both sign-in paths now feel the
 *  same. */
const POLL_MS = 2000;

/** Give the wait a deadline. Without one, an operator who opens the approval page and
 *  never approves leaves the browser polling forever, with the button stuck disabled
 *  until a full page reload. */
const DEADLINE_MS = 5 * 60 * 1000;

/** What both poll endpoints answer with. */
export interface PinPollResult {
  /** `retrying` is a NON-final status, like `pending`: the sign-in was approved but the
   *  Plex server did not answer this instant (it may be restarting). The backend keeps
   *  the sign-in alive and answers this instead of an error, because this loop stops for
   *  good on any thrown status -- which used to throw away a sign-in that was still
   *  perfectly good and send the operator back through the whole approval round trip. */
  status: "pending" | "retrying" | "ok" | "choose_server";
  servers: PlexServerChoice[] | null;
  /** Present only with status "retrying": why this poll couldn't finish yet, composed
   *  through `why.ts`'s `composeError` (phase 8b). */
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
  // run it belongs to and a finished run ignores whatever lands late (B-11). Without this, two
  // slow polls overlap: the second answers "ok" and signs the operator in, then the first
  // rejects (the PIN is now consumed, or plex.tv rate-limited us) and paints "Plex sign-in
  // failed" over a session that succeeded. Through `cancel` the mirror case signed the operator
  // in after they pressed Cancel. `begin`, `stop` and `cancel` all bump the run.
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
              // The sign-in stays valid while the picker is up; stop polling and hold
              // the list until the operator picks one.
              stop();
              setRetrying(null);
              setServers(result.servers ?? []);
              // Said here for the same reason the retry reason above is: this is the one copy
              // both sign-in paths share, so a third caller inherits the announcement instead
              // of forgetting it (rule 72) -- which is not hypothetical, since the login screen
              // is exactly the caller that forgot it. Told and NOT focused: the picker replaces
              // the wait on a timer, so moving focus here is a steal rather than a recovery.
              announce(CHOOSE_SERVER_SAID);
              h.onChooseServer?.(result.servers ?? []);
            } else {
              // "pending" or "retrying" -- neither is final, so keep polling. Only
              // "retrying" carries a reason; say it, so a longer-than-usual wait is
              // explained rather than looking like a hang.
              const reason =
                result.status === "retrying" && result.reason ? composeError(result.reason) : null;
              setRetrying(reason);
              // And say it by ear as well. Every transition in this flow is driven by the
              // two-second poll rather than by the operator, so this paragraph swaps its text
              // with nobody touching anything: on screen it changed, by ear it did not (#177).
              //
              // Announced from the hook and not from either caller's markup, for two reasons.
              // This is the one copy both sign-in paths share, so a third caller inherits the
              // announcement instead of forgetting it (rule 72). And the paragraph it replaces
              // holds a link, so making that a live region would put interactive content inside
              // one -- the shape #177 filed against the queue's nudge.
              //
              // Only on a CHANGE: the poll repeats the same reason every two seconds, and a
              // region restating it would talk over everything else on the page.
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

  /** The operator picked a server. One immediate poll usually finishes the job; a
   *  "pending" answer (plex.tv asking us to slow down) falls back to polling. */
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
          // The pick can land back on the picker (an account whose server list changed under
          // it), and that arrives the same way: on an answer, not on a press.
          announce(CHOOSE_SERVER_SAID);
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
