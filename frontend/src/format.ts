// SPDX-License-Identifier: AGPL-3.0-or-later

// Everything here formats through the locale i18next serves strings in, so a number, date
// or list never disagrees with the sentence around it (docs/history/I18N_PLAN.md, Stage 2).

import i18next from "./i18n";

/** One Intl formatter per locale, built on first use. The active locale is read on every
 *  call, so a language change simply stops hitting the old entry. */
function perLocale<T>(make: (locale: string) => T): () => T {
  const cache = new Map<string, T>();
  return () => {
    const locale = i18next.language;
    let made = cache.get(locale);
    if (made === undefined) {
      made = make(locale);
      cache.set(locale, made);
    }
    return made;
  };
}

// No grouping: the scaled value only reaches four digits at the PiB cap.
const wholeNumber = perLocale(
  (locale) => new Intl.NumberFormat(locale, { useGrouping: false, maximumFractionDigits: 0 }),
);
const oneDecimal = perLocale(
  (locale) =>
    new Intl.NumberFormat(locale, {
      useGrouping: false,
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }),
);

/** Bytes, in the units people actually reason about disk in.
 *
 *  Binary units (TiB), because that is what `df`, Sonarr and Radarr report. Showing 4.6 TB
 *  next to an *arr's 4.2 TiB for the same files would invite the owner to conclude Reaper
 *  miscounted, and they would be right to worry. */
export function bytes(value: number): string {
  if (value <= 0) return "0 B";

  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const exponent = Math.min(Math.floor(Math.log2(value) / 10), units.length - 1);
  const scaled = value / 1024 ** exponent;

  // One decimal below 100, none above: "5.9 GiB", "214 GiB".
  const digits = exponent === 0 || scaled >= 100 ? 0 : 1;
  return `${(digits ? oneDecimal() : wholeNumber()).format(scaled)} ${units[exponent]}`;
}

/** One item's size on disk, for the surfaces the operator scans while deciding.
 *
 *  `null` means nothing would report a size. The server says so directly, rather than
 *  sending a `0` for the client to guess about, so a real `0` still renders as "0 B",
 *  honestly.
 *
 *  Totals use `totalBytes` below, which carries the unknown count beside the sum. */
export function itemBytes(value: number | null): string {
  return value === null ? i18next.t("format.sizeUnknown") : bytes(value);
}

/** A total, plus how many items it could not include.
 *
 *  A sum with an unmeasured item in it reads quietly low, and low is the dangerous
 *  direction next to a delete control. So the sum covers what is known and the count
 *  rides alongside it, hidden entirely at zero: an operator whose sources all answer
 *  sees exactly what they saw before. */
export function totalBytes(known: number, unknown: number): string {
  if (unknown === 0) return bytes(known);
  return i18next.t("format.totalWithUnknown", { known: bytes(known), n: unknown });
}

const grouped = perLocale((locale) => new Intl.NumberFormat(locale));

export function count(value: number): string {
  return grouped().format(value);
}

/** A count sharing a line with a number the server already wrote into a sentence.
 *
 *  The server groups with a literal comma (Python's `:,`) and cannot know the browser's
 *  locale; `count` above follows the app's locale instead. Put the two in one sentence and a
 *  `de-DE` browser would read "1,234 added, 5,678 cleared. Last updated 5 minutes ago, 1.234
 *  movies …", two thousands separators in one line, neither wrong on its own. Every number on
 *  such a line goes through here instead, so the line agrees with itself. This pins to
 *  `en-US` grouping because the app's copy is American English throughout.
 *
 *  One caller today: `PlexPanel`'s shelf status line, whose leading sentence is
 *  `LeavingSoonResult.summary`. Reach for it wherever server-formatted text and a local count
 *  land in the same string, and prefer removing the mix over adding a second caller. */
export function countBesideServerText(value: number): string {
  return value.toLocaleString("en-US");
}

/** The Reaper's tally, singular-aware: "1 soul", "7 souls". Every reap surface counts in
 *  souls, not items, so the count and its noun stay together in one place. */
export function souls(value: number): string {
  return i18next.t("format.souls", { n: value });
}

/** Basis points to a percentage. Coverage is stored as basis points because the policy body
 *  is integers-only: floats do not canonicalize consistently, and an unstable hash would
 *  void approvals at random. */
export function coverage(bp: number): string {
  return `${Math.round(bp / 100)}%`;
}

type SpanUnit = "years" | "months" | "days";

/** The two most-significant non-zero units of a whole day count, largest first: `2060`
 *  becomes `[[5, "years"], [7, "months"]]`. The truncation and rounding `humanDays` and
 *  `humanWindow` both need; they differ only in how they word the result. */
function spanUnits(whole: number): [number, SpanUnit][] {
  const years = Math.floor(whole / 365);
  const months = Math.floor((whole % 365) / 30);
  const days = (whole % 365) % 30;
  const units: [number, SpanUnit][] = [
    [years, "years"],
    [months, "months"],
    [days, "days"],
  ];
  return units.filter(([n]) => n > 0).slice(0, 2);
}

/** "5 years", "1 month", "3 days": `format.span.<unit>`, pluralized on `n` through the
 *  catalog's own ICU plural rules rather than a hand-rolled trailing "s". */
function spanPhrase(n: number, unit: SpanUnit): string {
  if (unit === "years") return i18next.t("format.span.years", { n });
  if (unit === "months") return i18next.t("format.span.months", { n });
  return i18next.t("format.span.days", { n });
}

/** The bare unit word with no count, for `humanWindow`'s single-"1" case: "year", not
 *  "1 year". */
function spanWord(unit: SpanUnit): string {
  if (unit === "years") return i18next.t("format.span.yearWord");
  if (unit === "months") return i18next.t("format.span.monthWord");
  return i18next.t("format.span.dayWord");
}

/** A day count as a phrase a person reads without doing arithmetic: `2060` becomes
 *  "5 years, 7 months".
 *
 *  This is a port of `clock.humanize_days`, not a second design, because the two sit side by
 *  side on the policy page: the server words the history warnings and this words the controls
 *  beside them. If one said "1 year, 1 month" while the other said "400 days" about the same
 *  number, a page whose whole job is to be read would contradict itself. `humanDays.test.ts`
 *  pins the two against the same table.
 *
 *  Kept to the two most-significant units on purpose, and approximate by construction (a
 *  month is 30 days, a year 365): these are phrases beside a dormancy setting, not
 *  accounting.
 *
 *  Lives here rather than in `PolicyEditor` because `signalRamp` needs it too, and the editor
 *  imports `signalRamp`; importing this the other way around would create a cycle.
 *
 *  The unit words ("year"/"month"/"day") and the sub-day floor ("less than a day") are
 *  catalog entries under `format.span.*`, so they translate; only the truncation, rounding
 *  and two-unit join stay in TS. */
export function humanDays(days: number): string {
  const whole = Math.round(days);
  if (whole <= 0) return i18next.t("format.span.lessThanADay");
  return spanUnits(whole)
    .map(([n, unit]) => spanPhrase(n, unit))
    .join(", ");
}

/** A window length phrased for "in the last {window}": like `humanDays`, except a
 *  single-unit window drops the redundant "1", so it reads "in the last year", not "in the
 *  last 1 year". A multi-unit window ("1 year, 6 months") keeps it. The wording mirrors
 *  Python's `clock.humanize_window`, whose sentences now live in the catalog
 *  (docs/history/I18N_PLAN.md §5). */
export function humanWindow(days: number): string {
  const whole = Math.round(days);
  if (whole <= 0) return i18next.t("format.span.lessThanADay");
  const units = spanUnits(whole);
  const only = units.length === 1 ? units[0] : undefined;
  if (only && only[0] === 1) return spanWord(only[1]);
  return units.map(([n, unit]) => spanPhrase(n, unit)).join(", ");
}

const dayFormat = perLocale(
  (locale) => new Intl.DateTimeFormat(locale, { year: "numeric", month: "short", day: "numeric" }),
);

// `Intl.DateTimeFormat.format` throws on an invalid date, unlike `toLocaleDateString`, which
// renders "Invalid Date" instead. One bad timestamp must not unmount the panel around it, so
// the raw value is shown as the honest degraded state, and the operator can quote it in a
// report.
export function date(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : dayFormat().format(parsed);
}

const clockFormat = perLocale(
  (locale) => new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit" }),
);

/** The clock time, for the surfaces that want the hour a thing happened, not just the day. */
export function time(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : clockFormat().format(parsed);
}

const listFormat = perLocale(
  (locale) => new Intl.ListFormat(locale, { style: "long", type: "conjunction" }),
);

/** "a, b, and c", in the locale's own list grammar. */
export function list(items: string[]): string {
  return listFormat().format(items);
}

const weekdayFormat = perLocale(
  (locale) => new Intl.DateTimeFormat(locale, { weekday: "long", timeZone: "UTC" }),
);

/** A weekday's name from its cron index, 0 = Sunday. */
export function weekday(dayIndex: number): string {
  // 2023-01-01 was a Sunday, so day 1 + index lands on the named weekday.
  return weekdayFormat().format(new Date(Date.UTC(2023, 0, 1 + dayIndex)));
}

const relative = perLocale((locale) => new Intl.RelativeTimeFormat(locale, { numeric: "auto" }));

/** How long ago, in the terms an operator actually makes decisions in: minutes and hours on
 *  the day it happened, then days, months and years.
 *
 *  A freshness line has to tell a scan five minutes old apart from one from this morning,
 *  since staleness is the whole reason the line exists. Collapsing both to a single word
 *  like "today" would lose that. */
export function since(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return iso;

  const days = Math.floor(ms / 86_400_000);
  if (days < 1) {
    // Rounded to the minute, floored to the hour: "8 minutes ago" reading as 7 would be
    // wrong, while an hour count that rounds up could say 24 hours on a stamp that is still
    // today.
    const minutes = Math.round(ms / 60_000);
    if (minutes < 1) return relative().format(0, "second");
    if (minutes < 60) return relative().format(-minutes, "minute");
    return relative().format(-Math.floor(minutes / 60), "hour");
  }
  if (days === 1) return relative().format(-1, "day");
  if (days < 60) return relative().format(-days, "day");
  if (days < 730) return relative().format(-Math.floor(days / 30), "month");
  // One decimal past two years ("2.4 years ago"); the formatter drops a trailing .0.
  return relative().format(-Number((days / 365).toFixed(1)), "year");
}

/** A spare is always set to a whole number of days on the server (`now + N days`), so its
 *  true remaining life is at most N and counts down. When Reaper's server clock runs a little
 *  ahead of the browser's, the normal case when server and browser are different machines, the
 *  browser reads a hair more than N and would round a fresh "90 days" spare up to "91d". This
 *  absorbs up to an hour of that lead before rounding up: far more than any real clock skew,
 *  yet far less than a day, so a genuine partial day still rounds up correctly. */
const SPARE_SKEW_SLACK_MS = 3_600_000;

/** How a timed hand-spare's remaining life reads on a card. `iso` is when the spare stops
 *  keeping the item; null means it never does (kept forever). While time remains, `days` is
 *  at least 1, and reaches 0 only once expired.
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
 *  A scan is what actually realizes the expiry (`whitelist.purge_expired_spares`), so past
 *  the date the item is still genuinely kept: the planner, the ledger and the executor all go
 *  on reading every spare on file. That is why `expired` is a state the UI draws instead of
 *  hiding: `note` is the sentence that says so, and `until` empties out so no caller can
 *  promise "Kept until" a day that has already gone by. */
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
   *  once expired, since past the date it would be a promise about a day already gone. The
   *  expired state says its piece through `note` instead. */
  until: string;
  /** The whole sentence an expired spare needs, for a tooltip or the why panel: what happened,
   *  and that the file is still kept until a scan judges it again. Empty in every other state.
   *  One derivation, so the button, the chip and the panel cannot word it three different
   *  ways. */
  note: string;
  /** Just the fact, without `note`'s claim about what happens next: "Your spare expired on
   *  Aug 18". For surfaces that know only this item's own spare, and so cannot say whether
   *  anything still keeps the file, like the Spare button, whose item may sit inside a show
   *  spare that outlasts it. `note` is right wherever the covering spare is this one. Empty in
   *  every other state, with the same date wording as `note`, from the one derivation. */
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
    const expiredOn = i18next.t("format.spareExpiredOn", { date: date(iso) });
    return {
      forever: false,
      days: 0,
      expired: true,
      short: i18next.t("format.spareShort", { n: 0 }),
      phrase: i18next.t("format.spareExpiredPhrase"),
      until: "",
      note: i18next.t("format.spareStillKept", { expiredOn }),
      expiredOn,
    };
  }
  // Round up so a partial day still shows, but only after shaving the small clock-skew slack.
  // Otherwise a fresh N-day spare would read N+1. Floor at 1: while time remains it is never
  // "0 days".
  const days = Math.max(1, Math.ceil((ms - SPARE_SKEW_SLACK_MS) / 86_400_000));
  return {
    forever: false,
    days,
    expired: false,
    short: i18next.t("format.spareShort", { n: days }),
    phrase: i18next.t("format.spareLeft", { n: days }),
    until: i18next.t("format.spareUntil", { date: date(iso) }),
    note: "",
    expiredOn: "",
  };
}

/** Whether a stored title already ends in its own release year, as some do ("Some Show (2019)").
 *
 *  One derivation with two readers, which is the point: the Scales row uses it to decide whether
 *  to print the year in its own span, and `titleWithYear` below uses it to build the string a
 *  jump prefills the review search with. Those two have to agree, or the search box would be
 *  seeded with a year the row is not showing. */
export function carriesYear(title: string, year: number | null | undefined): boolean {
  return year != null && title.trim().endsWith(`(${year})`);
}

/** A title the way the operator reads it on screen, year and all. What a jump seeds the
 *  review search box with.
 *
 *  Spelled the way the QUEUE prints it ("Example Alpha 1979"), because the queue is where the
 *  jump lands and the seeded text sits above its cards. Scales prints the same fact in
 *  parentheses, and `list_candidates` (api/review.py) understands either, so the two spellings
 *  are a display choice rather than something that has to be reconciled. */
export function titleWithYear(title: string, year: number | null | undefined): string {
  const name = title.trim();
  if (year == null || carriesYear(name, year)) return name;
  return `${name} ${year}`;
}
