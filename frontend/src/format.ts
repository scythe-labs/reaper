// SPDX-License-Identifier: AGPL-3.0-or-later

/** Bytes, in the units people actually reason about disk in.
 *
 *  Binary units (TiB), because that is what `df`, Sonarr and Radarr report -- showing
 *  4.6 TB next to an *arr's 4.2 TiB for the same files invites the owner to conclude
 *  Reaper has miscounted, and they would be right to worry. */
export function bytes(value: number): string {
  if (value <= 0) return "0 B";

  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const exponent = Math.min(Math.floor(Math.log2(value) / 10), units.length - 1);
  const scaled = value / 1024 ** exponent;

  // One decimal below 100, none above: "5.9 GiB", "214 GiB".
  const digits = exponent === 0 || scaled >= 100 ? 0 : 1;
  return `${scaled.toFixed(digits)} ${units[exponent]}`;
}

/** One item's size on disk, for the surfaces the operator scans while deciding.
 *
 *  `null` means nothing would report a size, and the server says so directly now rather
 *  than sending a `0` for the client to guess about. A real `0` therefore renders as
 *  "0 B" again, honestly.
 *
 *  Totals use `totalBytes` below, which carries the unknown count beside the sum. */
export function itemBytes(value: number | null): string {
  return value === null ? "Size unknown" : bytes(value);
}

/** A total, plus how many items it could not include.
 *
 *  A sum with an unmeasured item in it is quietly low, and "low" is the dangerous
 *  direction beside a delete control. So the sum covers what is known and the count
 *  rides alongside it, suppressed entirely at zero: an operator whose sources all
 *  answer sees exactly what they saw before. */
export function totalBytes(known: number, unknown: number): string {
  if (unknown === 0) return bytes(known);
  return `${bytes(known)} · ${count(unknown)} ${unknown === 1 ? "size" : "sizes"} unknown`;
}

export function count(value: number): string {
  return value.toLocaleString();
}

/** The Reaper's tally, singular-aware: "1 soul", "7 souls". Every reap surface counts in
 *  souls, not items, so the count and its noun stay together in one place. */
export function souls(value: number): string {
  return `${value.toLocaleString()} ${value === 1 ? "soul" : "souls"}`;
}

/** Basis points to a percentage. Coverage is stored as bp because the policy body is
 *  integers-only -- floats do not canonicalise, and an unstable hash would void
 *  approvals at random. */
export function coverage(bp: number): string {
  return `${Math.round(bp / 100)}%`;
}

export function date(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** The clock time, for the surfaces that want the hour a thing happened, not just the day. */
export function time(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** How long ago, in the coarse terms the decisions are actually made in. */
export function since(iso: string): string {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);

  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 60) return `${days} days ago`;
  if (days < 730) return `${Math.floor(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}

/** A spare is always set to a WHOLE number of days on the server (`now + N days`), so its true
 *  remaining life is at most N and counts down. When Reaper's server clock runs a little ahead
 *  of the browser's -- the normal self-hosted case, server and browser on different machines --
 *  the browser reads a hair MORE than N and would round a fresh "90 days" spare up to "91d". We
 *  absorb up to an hour of that lead before rounding up, which is far more than any sane clock
 *  skew yet far less than the day granularity, so a genuine partial day is untouched. */
const SPARE_SKEW_SLACK_MS = 3_600_000;

/** How a timed hand-spare's remaining life reads on a card. `iso` is when the spare stops
 *  keeping the item; null means it never does (kept forever). While time remains `days` is at
 *  least 1, and reaches 0 only once expired.
 *
 *  Three states, and each field is filled for exactly the ones that may print it, so a surface
 *  can never render a string belonging to a state it is not in:
 *
 *  | state    | `short` | `phrase`      | `until`           | `note` / `expiredOn`       |
 *  | -------- | ------- | ------------- | ----------------- | -------------------------- |
 *  | forever  | `""`    | `""`          | `""`              | `""`                       |
 *  | counting | `"27d"` | `27 days left`| `Kept until Aug 18`| `""`                      |
 *  | expired  | `"0d"`  | `expired`     | `""`              | `Your spare expired on ...`|
 *
 *  The expiry is only realized by a SCAN (`whitelist.purge_expired_spares`), so past the date
 *  the item really is still kept -- the planner, the ledger and the executor all go on reading
 *  every spare on file. That is why `expired` is a state the UI draws rather than a state it
 *  hides: `note` is the sentence that says so, and `until` empties out precisely so no caller
 *  can promise "Kept until" a day that has already gone by. */
export function spareRemaining(iso: string | null): {
  forever: boolean;
  days: number;
  expired: boolean;
  /** The compact count the resting mark and the Spare button wear: "27d", "0d" once expired.
   *  Empty for a forever spare, which counts nothing. */
  short: string;
  /** The chip's clause: "27 days left", "1 day left", "expired". Empty for a forever spare,
   *  whose chip claims the keep outright instead ("will be kept"). */
  phrase: string;
  /** "Kept until Aug 18", for a tooltip or a fuller line. Empty for a forever spare, and empty
   *  once expired -- past the date it would be a promise about a day already gone. The expired
   *  state says its piece through `note`. */
  until: string;
  /** The whole sentence an expired spare needs, for a tooltip or the why panel: what happened,
   *  and that the file is still kept until a scan judges it again. Empty in every other state.
   *  One derivation, so the button, the chip and the panel cannot word it three ways (rule
   *  104). */
  note: string;
  /** Just the fact, without `note`'s claim about what happens next: "Your spare expired on
   *  Aug 18". For the surfaces that know only this item's OWN spare and so cannot say whether
   *  anything still keeps the file -- the Spare button, whose item may sit inside a show spare
   *  that outlasts it. `note` is right wherever the covering spare IS this one. Empty in every
   *  other state, and the same date wording as `note`, from the one derivation. */
  expiredOn: string;
} {
  if (!iso) {
    return {
      forever: true,
      days: 0,
      expired: false,
      short: "",
      phrase: "",
      until: "",
      note: "",
      expiredOn: "",
    };
  }
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) {
    const expiredOn = `Your spare expired on ${date(iso)}`;
    return {
      forever: false,
      days: 0,
      expired: true,
      short: "0d",
      phrase: "expired",
      until: "",
      note: `${expiredOn}. Still kept until the next scan judges it again`,
      expiredOn,
    };
  }
  // Round up so a partial day still shows, but only after shaving the small clock-skew slack --
  // otherwise a fresh N-day spare reads N+1. Floor at 1: while time remains it is never "0 days".
  const days = Math.max(1, Math.ceil((ms - SPARE_SKEW_SLACK_MS) / 86_400_000));
  return {
    forever: false,
    days,
    expired: false,
    short: `${days}d`,
    phrase: days === 1 ? "1 day left" : `${days} days left`,
    until: `Kept until ${date(iso)}`,
    note: "",
    expiredOn: "",
  };
}
