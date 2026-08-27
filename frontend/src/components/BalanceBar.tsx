// SPDX-License-Identifier: AGPL-3.0-or-later
//
// How much of what a person asked for is earning its keep, as one bar.
//
// The card and the panel drew this bar identically, down to the `aria-label`. That label is
// the part worth keeping in one place. The bar is `role="img"`, so the label is the ONLY thing
// a screen reader gets from it, and two copies of it are two chances for one to stop matching
// the legend beside it.

import { useTranslation } from "react-i18next";
import { bytes } from "../format";

/** The kept/reclaimable split for one requester.
 *
 *  `granted` of zero means no size is known for anything they asked for. The bar then goes full
 *  and neutral instead of dividing by zero. The reclaim segment is dropped rather than drawn at
 *  zero width. */
export function BalanceBar({
  granted,
  reclaim,
  hasReclaim,
}: {
  granted: number;
  reclaim: number;
  /** Whether the scan found anything to reclaim. This is a COUNT of items, while the bar's own
   *  numbers are bytes, so a title with no known size still counts as reclaimable even though it
   *  adds zero width to the bar. Pass it directly rather than deriving it from `reclaim`. */
  hasReclaim: boolean;
}) {
  const { t } = useTranslation();
  const used = Math.max(0, granted - reclaim);
  const usedPct = granted > 0 ? (100 * used) / granted : 100;
  const reclaimPct = granted > 0 ? (100 * reclaim) / granted : 0;
  return (
    <div
      className="fair-balance"
      role="img"
      aria-label={
        hasReclaim
          ? t("scales.balanceBar.ariaReclaim", { used: bytes(used), reclaim: bytes(reclaim) })
          : t("scales.balanceBar.ariaNoReclaim", { used: bytes(used) })
      }
    >
      <i className="used" style={{ width: `${usedPct}%` }} />
      {reclaimPct > 0 && <i className="reclaim" style={{ width: `${reclaimPct}%` }} />}
    </div>
  );
}
