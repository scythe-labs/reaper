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
import type { PlexServerChoice } from "../api";

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
  status: "pending" | "ok" | "choose_server";
  servers: PlexServerChoice[] | null;
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
  return e instanceof Error ? e.message : "Plex sign-in failed.";
}

/** Drive one PIN through to a final answer.
 *
 *  `begin` starts polling for a PIN that has been opened for approval, `pick` carries the
 *  operator's server choice, and `cancel` stops everything. Any final outcome (ok, timed
 *  out, failed) stops the timer and drops the server list before the handler runs. */
export function usePlexPinPoll<R extends PinPollResult>(handlers: PinPollHandlers<R>) {
  const [servers, setServers] = useState<PlexServerChoice[] | null>(null);
  const timerRef = useRef<number | null>(null);
  const pinRef = useRef<number | null>(null);

  // The running timer reads the handlers through a ref, so a re-render never restarts the
  // poll and the callback never calls a stale closure.
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  const stop = useCallback(() => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = null;
  }, []);

  useEffect(() => stop, [stop]);

  const begin = useCallback(
    (pinId: number, machineId?: string) => {
      stop();
      pinRef.current = pinId;
      const deadline = Date.now() + DEADLINE_MS;
      timerRef.current = window.setInterval(() => {
        const h = handlersRef.current;
        if (Date.now() > deadline) {
          stop();
          setServers(null);
          h.onTimedOut();
          return;
        }
        void (async () => {
          try {
            const result = await h.poll(pinId, machineId);
            if (result.status === "ok") {
              stop();
              setServers(null);
              h.onOk(result);
            } else if (result.status === "choose_server") {
              // The sign-in stays valid while the picker is up; stop polling and hold
              // the list until the operator picks one.
              stop();
              setServers(result.servers ?? []);
              h.onChooseServer?.(result.servers ?? []);
            }
          } catch (e) {
            stop();
            setServers(null);
            h.onFailed(failureText(e));
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
      const h = handlersRef.current;
      try {
        const result = await h.poll(pinId, machineId);
        if (result.status === "ok") {
          h.onOk(result);
        } else if (result.status === "choose_server") {
          setServers(result.servers ?? []);
          h.onChooseServer?.(result.servers ?? []);
        } else {
          begin(pinId, machineId);
        }
      } catch (e) {
        h.onFailed(failureText(e));
      }
    },
    [begin],
  );

  const cancel = useCallback(() => {
    stop();
    setServers(null);
  }, [stop]);

  return { servers, begin, pick, cancel };
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
        Cancel
      </button>
    </>
  );
}
