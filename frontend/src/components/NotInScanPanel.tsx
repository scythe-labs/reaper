// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The board's "not in the last scan" panel, opened from the amber Scales tile. It is the
// why-panel shell (`.why`, `main.split`) reused wholesale -- the same side-column / right-hand
// sheet behavior and head/close grammar as ScalesPanel -- so the whole screen keeps one panel
// idiom. Its body is the shared UnmatchedList: every not-in-scan request across everyone,
// grouped by why. It decides nothing.

import { useId } from "react";
import { useTranslation } from "react-i18next";
import type { UnmatchedRequest } from "../api";
import { UnmatchedList } from "./UnmatchedList";
import { WhyShell } from "./WhyShell";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";

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
  const { t } = useTranslation();
  const headingId = useId();

  // The card counts requests; the panel merges co-requesters into one row per title. Reconcile
  // the two out loud so a smaller row count never reads as "some are missing".
  const titles = items.length;
  const requests = items.reduce((sum, u) => sum + u.request_count, 0);
  const lead =
    titles === requests
      ? t("scales.notInScan.leadSame", { n: requests })
      : t("scales.notInScan.leadSplit", { requests, n: titles });

  // A failed read is only the whole answer while there is nothing to show. React Query keeps
  // the last good report through a failed refetch -- and EVERY override invalidates
  // ["fairness"] (useOverrideMutations), so sparing one title reaches this -- which left the
  // panel replacing a list it was still holding with "couldn't check" (#190). An empty `items`
  // with the read failed stays the failure, never "nothing was left out": a genuinely empty
  // report and an unread one are indistinguishable here, and claiming the all-clear is the
  // wrong direction to guess in.
  const unread = error && items.length === 0;
  const sub = isPending
    ? t("common.checkingLastScan")
    : unread
      ? t("scales.notInScan.unread")
      : items.length === 0
        ? t("scales.notInScan.nothingLeftOut")
        : t("scales.notInScan.leadWithDetail", { lead });

  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      <div className="why-head">
        <div>
          <h2 id={headingId}>{t("scales.notInScan.heading")}</h2>
          <p className="why-sub muted">{sub}</p>
        </div>
      </div>

      {isPending ? (
        <p className="scales-foot muted">{t("common.loading")}</p>
      ) : unread ? (
        <Notice tone="error">{t("scales.notInScan.readFailed")}</Notice>
      ) : items.length === 0 ? (
        <p className="scales-foot">{t("scales.notInScan.allIncluded")}</p>
      ) : (
        <div className="block">
          {error && <StaleReadNotice what={t("scales.notInScan.staleWhat")} />}
          <UnmatchedList items={items} />
        </div>
      )}
    </WhyShell>
  );
}
