// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one status chip a card (or season row) wears. The text arrives display-ready
// from the server -- the protection that keeps a Sanctuary item, or what stopped Reaper
// short on a Limbo one -- so this only picks the tone's color: green for "kept", gray
// for "nothing to act on", amber outline for "deliberately left for you to decide".
//
// The chips an owner's own decision puts on an item live here too, so the queue and the
// show panel say the same words about a spare or a reap.

import type { Chip, Override } from "../api";

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

/** The reason text behind a kept row's chip ("Kept · playing right now" -> "playing
 *  right now"), for the refused-reap chip's honest wording. */
export function chipWhy(chip: Chip | null): string | null {
  if (!chip) return null;
  return chip.text.replace(/^Kept · /, "");
}

/** Which class family an override chip is drawn in. Cards use the `.chip` family that
 *  sits on a card's meta line; the season rows in the show panel use the `.status-chip`
 *  family they share with the scan chip they replace. The wording is the same either way,
 *  which is the point of having one component. */
export type ChipFamily = "chip" | "status-chip";

const OVERRIDE_CLASSES: Record<ChipFamily, { spare: string; refused: string; reap: string }> = {
  chip: {
    spare: "chip chip-hand-spare",
    refused: "chip chip-reap-refused",
    reap: "chip chip-hand-reap",
  },
  "status-chip": {
    spare: "status-chip status-hand-spare",
    refused: "status-chip status-look",
    reap: "status-chip status-hand-reap",
  },
};

/** The chip an item shows once the owner has overridden it by hand. Solid fills are the
 *  owner's decisions; outlined chips are Reaper's. A reap takes effect immediately --
 *  counts, grace countdown, the next plan -- unless the engine refuses it (someone is
 *  watching right now, or the file isn't managed), which reads amber and says why. */
export function OverrideChip({
  override,
  effective,
  keptWhy,
  family = "chip",
}: {
  override: Override | null;
  effective?: boolean | null | undefined;
  keptWhy?: string | null | undefined;
  family?: ChipFamily;
}) {
  const classes = OVERRIDE_CLASSES[family];
  if (override === "spare") {
    return <span className={classes.spare}>Spared by hand · will be kept</span>;
  }
  if (override !== "reap") return null;
  if (effective === false) {
    return (
      <span className={classes.refused}>
        Reap requested · kept for now: {keptWhy ?? "a safety stop applies"}
      </span>
    );
  }
  return <span className={classes.reap}>Reaped by hand · will be removed</span>;
}
