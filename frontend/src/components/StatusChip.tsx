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

/** The refused-reap chip's clause: why the item is still being kept, in lowercase, ready
 *  to sit after "Reap requested · kept for now:".
 *
 *  A reap is refused in two different lanes, and their chips read nothing alike. A
 *  protection that fired says "Kept · playing right now", so dropping the prefix leaves a
 *  clause that already fits. A reap refused because a protection could not be checked
 *  carries that lane's own chip instead, capitalised and sometimes with a middot of its
 *  own, so each of those is mapped to a clause here rather than pasted in mid-sentence.
 *  Both lanes come from `decide_verdict`'s reap branch (blocked, or a safety stop). */
const BLOCKED_WHY: Record<string, string> = {
  "Couldn't be found in Plex": "it couldn't be found in Plex",
  "Looks like two different things in Plex": "it looks like two different things in Plex",
  "Needs a look · watched more than a season your rule keeps":
    "watched more than a season your rule keeps",
  "Needs a look · left for you to decide": "a check on it couldn't be settled",
  "Some checks couldn't run": "some checks couldn't run",
};

export function chipWhy(chip: Chip | null): string | null {
  if (!chip) return null;
  if (chip.text.startsWith("Kept · ")) return chip.text.slice("Kept · ".length);
  return BLOCKED_WHY[chip.text] ?? null;
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
 *  owner's decisions; outlined chips are Reaper's. A reap takes effect on the server the
 *  moment it is saved, and the views that show it (the queue and its counts, the show
 *  panel, the why panel, the grace countdown) refresh together through
 *  `useOverrideMutations`' refresh(). The engine still refuses a reap it must not honour
 *  (someone is watching right now, the file isn't managed, or a protection could not be
 *  checked); that reads amber and says why, via `chipWhy` above. */
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
        Reap requested · kept for now: {keptWhy ?? "Reaper couldn't confirm it's safe to remove"}
      </span>
    );
  }
  return <span className={classes.reap}>Reaped by hand · will be removed</span>;
}
