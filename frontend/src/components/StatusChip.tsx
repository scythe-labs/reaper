// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one status chip a card (or season row) wears. The text arrives display-ready
// from the server -- the protection that keeps a Sanctuary item, or what stopped Reaper
// short on a Limbo one -- so this only picks the tone's color: green for "kept", gray
// for "nothing to act on", amber outline for "deliberately left for you to decide".

import type { Chip } from "../api";

export function StatusChip({ chip }: { chip: Chip | null }) {
  if (!chip) return null;
  return (
    <span className={`status-chip status-${chip.tone}`} title={chip.text}>
      {chip.text}
    </span>
  );
}

/** The red mark a condemned season wears in the all-seasons list, where rows from
 *  every lane sit side by side. Condemned rows carry no server chip (their card leads
 *  with the amber dormancy pill), so the list states their fate with this constant. */
export function CondemnedChip() {
  return <span className="status-chip status-pressure">Would be removed</span>;
}
