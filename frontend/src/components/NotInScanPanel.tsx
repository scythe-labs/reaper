// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The board's "not in the last scan" panel, opened from the amber Scales tile. It is the
// why-panel shell (`.why`, `main.split`) reused wholesale -- the same side-column / right-hand
// sheet behavior and head/close grammar as ScalesPanel -- so the whole screen keeps one panel
// idiom. Its body is the shared UnmatchedList: every not-in-scan request across everyone,
// grouped by why. It decides nothing.

import { useEffect } from "react";
import type { UnmatchedRequest } from "../api";
import { count } from "../format";
import { UnmatchedList } from "./UnmatchedList";

export function NotInScanPanel({
  items,
  isPending = false,
  error = false,
  onClose,
}: {
  items: UnmatchedRequest[];
  // This panel is always-visible once opened, so a bare items array is not enough: a pending or
  // failed report must read as "we could not look," never collapse to the empty all-clear below
  // (rules 17/36). The caller passes the query's own loading and error state.
  isPending?: boolean;
  error?: boolean;
  onClose: () => void;
}) {
  // Escape closes, matching the why-panel; a modal, if one is up, owns the key first.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (document.querySelector('[role="dialog"]')) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The card counts requests; the panel merges co-requesters into one row per title. Reconcile
  // the two out loud so a smaller row count never reads as "some are missing".
  const titles = items.length;
  const requests = items.reduce((sum, u) => sum + u.request_count, 0);
  const lead =
    titles === requests
      ? `${count(requests)} ${requests === 1 ? "request" : "requests"} the last scan didn't include.`
      : `${count(requests)} requests across ${count(titles)} ${
          titles === 1 ? "title" : "titles"
        } the last scan didn't include.`;

  const sub = isPending
    ? "Checking the last scan…"
    : error
      ? "Couldn't check the last scan."
      : items.length === 0
        ? "Nothing was left out."
        : `${lead} Here is each one, and why.`;

  return (
    <aside className="why">
      <header className="why-head">
        <div>
          <h2>Not in the last scan</h2>
          <p className="why-sub muted">{sub}</p>
        </div>
        <button className="ghost why-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>

      {isPending ? (
        <p className="scales-foot muted">Loading…</p>
      ) : error ? (
        <p className="notice notice-error">
          Reaper couldn't read the last scan, so it can't say which requests were left out. Reload
          the page to try again.
        </p>
      ) : items.length === 0 ? (
        <p className="scales-foot">Every available request is in the last scan.</p>
      ) : (
        <div className="block">
          <UnmatchedList items={items} />
        </div>
      )}
    </aside>
  );
}
