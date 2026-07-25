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
import { spareRemaining } from "../format";

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
 *  A reap is refused in two different lanes, and their chips read nothing alike -- a
 *  protection that fired ("Kept · playing right now") and a check that could not run
 *  ("Some checks couldn't run") -- so the server words the clause for both beside the
 *  chip text itself (`_chip` in `api/routes.py`). This used to slice the "Kept · " prefix
 *  off and look the rest up in a map of the backend's exact prose: rewording one chip
 *  server-side dropped every held-reap explanation to the generic fallback below, with
 *  the tests still green because they transcribed the same strings (H-1). Null is a real
 *  answer -- a chip about the score names no reason a reap would be refused. */
export function chipWhy(chip: Chip | null): string | null {
  return chip?.why ?? null;
}

/** Which class family an override chip is drawn in. Cards use the `.chip` family that
 *  sits on a card's meta line; the season rows in the show panel use the `.status-chip`
 *  family they share with the scan chip they replace. The wording is the same either way,
 *  which is the point of having one component. */
export type ChipFamily = "chip" | "status-chip";

const OVERRIDE_CLASSES: Record<
  ChipFamily,
  { spare: string; spareExpired: string; refused: string; reap: string }
> = {
  chip: {
    spare: "chip chip-hand-spare",
    spareExpired: "chip chip-spare-expired",
    refused: "chip chip-reap-refused",
    reap: "chip chip-hand-reap",
  },
  "status-chip": {
    spare: "status-chip status-hand-spare",
    spareExpired: "status-chip status-spare-expired",
    refused: "status-chip status-reap-held",
    reap: "status-chip status-hand-reap",
  },
};

/** The chip an item shows once the owner has overridden it by hand. Solid fills are the
 *  owner's decisions; outlined chips are Reaper's. A reap takes effect on the server the
 *  moment it is saved, and the views that show it (the queue and its counts, the show
 *  panel, the why panel, the grace countdown) refresh together through
 *  `useOverrideMutations`' refresh(). The engine still refuses a reap it must not honor
 *  (someone is watching right now, the file isn't managed, or a protection could not be
 *  checked); that reads dashed red (held, not a done removal) and says why, via `chipWhy`
 *  above. Amber is reserved for "left for you to decide" and never marks a held reap. */
export function OverrideChip({
  override,
  effective,
  keptWhy,
  exceptions = 0,
  spareExpiresAt = null,
  family = "chip",
}: {
  override: Override | null;
  effective?: boolean | null | undefined;
  keptWhy?: string | null | undefined;
  /** For a whole-show chip: how many seasons carry their OWN opposing decision. When any do,
   *  the chip drops its unqualified "will be kept/removed" claim, since a season inside goes
   *  the other way (U-3). Zero for movies and single seasons. */
  exceptions?: number;
  /** When a *timed* spare stops keeping this item (ISO), or null for a forever spare. Read only
   *  on a spare chip: it turns "will be kept" into a countdown ("27 days left"), and then into
   *  "expired" on the dashed fill once the clock has passed. */
  spareExpiresAt?: string | null;
  family?: ChipFamily;
}) {
  const classes = OVERRIDE_CLASSES[family];
  const except =
    exceptions > 0 ? `except ${exceptions} ${exceptions === 1 ? "season" : "seasons"}` : null;
  if (override === "spare") {
    // A forever spare keeps "will be kept"; a timed one counts down; an expired one says so.
    // The exceptions clause, when present, still wins -- a mixed whole-show claim needs
    // qualifying first, and then the countdown is not the thing to lead with.
    //
    // An expired spare is NOT a spare that has stopped working: the item is still kept until a
    // scan realizes the clock, so the chip stays in the spare family and keeps saying "Spared
    // by hand". What changes is the clause and the fill -- dashed rather than solid, because
    // this is no longer a live decision. `note` carries the rest into the tooltip; `until` is
    // empty here by construction, so the chip can never promise a keep-until day already gone.
    const remaining = spareRemaining(spareExpiresAt);
    const suffix = except ?? (remaining.forever ? "will be kept" : remaining.phrase);
    const title = remaining.note || remaining.until;
    return (
      <span
        className={remaining.expired ? classes.spareExpired : classes.spare}
        title={title || undefined}
      >
        {suffix ? `Spared by hand · ${suffix}` : "Spared by hand"}
      </span>
    );
  }
  if (override !== "reap") return null;
  if (effective === false) {
    return (
      <span className={classes.refused}>
        Reap requested · kept for now: {keptWhy ?? "Reaper couldn't confirm it's safe to remove"}
      </span>
    );
  }
  return <span className={classes.reap}>Reaped by hand · {except ?? "will be removed"}</span>;
}
