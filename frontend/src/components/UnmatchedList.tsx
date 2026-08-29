// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The "not in the last scan" list: every available request the scan didn't judge, grouped by
// why, so the operator can see what each one is and why it wasn't matched. Shared by two
// surfaces: the board's own panel (NotInScanPanel, opened from the amber tile) and each
// person's drawer (ScalesPanel, where it replaces the old "N not listed here" footer). Both
// read the same way. It decides nothing: like the rest of Scales, it explains.
//
// A row never opens anything (there is no candidate to open), so it rests as static text,
// reusing the requested-title row chrome. Titles are looked up server-side. When one is
// missing, the row falls back to its type and date, never an id.

import { useTranslation } from "react-i18next";
import type { UnmatchedRequest } from "../api";
import { date } from "../format";
import i18next from "../i18n";
import { PosterFallback } from "./PosterFallback";

/** The reason groups, in reading order: the benign-and-expected first (a rescan fixes it),
 *  then the ones worth a look. Each names itself and carries one bound line explaining what
 *  it means for their files. A colored left stripe encodes the reason at a glance.
 *  Heading and why text live in the catalog, keyed by `reasonHeading`/`reasonWhy` below,
 *  because a plain data array can't hold translated strings: a language change wouldn't be
 *  picked up on re-render (same reason `reasonLabel` in ReapBreakdown.tsx is a function). */
const REASONS: { key: string; cls: string }[] = [
  { key: "after_scan", cls: "nis-group--new" },
  { key: "set_aside", cls: "nis-group--aside" },
  { key: "no_id", cls: "nis-group--nomatch" },
];

const KNOWN = new Set(REASONS.map((r) => r.key));

function reasonHeading(key: string): string {
  switch (key) {
    case "after_scan":
      return i18next.t("scales.unmatchedList.reasons.afterScan.heading");
    case "set_aside":
      return i18next.t("scales.unmatchedList.reasons.setAside.heading");
    case "no_id":
      return i18next.t("scales.unmatchedList.reasons.noId.heading");
    default:
      return key;
  }
}

function reasonWhy(key: string): string {
  switch (key) {
    case "after_scan":
      return i18next.t("scales.unmatchedList.reasons.afterScan.why");
    case "set_aside":
      return i18next.t("scales.unmatchedList.reasons.setAside.why");
    case "no_id":
      return i18next.t("scales.unmatchedList.reasons.noId.why");
    default:
      return "";
  }
}

/** Who asked, in plain words. On the board it names the requesters ("by A, B"). Inside one
 *  person's drawer their own name is dropped and it reads "also asked by X", matching the
 *  in-scan rows, so a shared title never looks like it was theirs alone. Long lists trail off
 *  as "and N others" rather than running off the row. */
function requesterLabel(names: string[], excludeName?: string): string | null {
  const others = excludeName ? names.filter((n) => n !== excludeName) : names;
  if (others.length === 0) return null;
  const shown = others.slice(0, 2).join(", ");
  const extra = others.length - 2;
  const list = extra > 0 ? i18next.t("scales.meta.andOthers", { shown, n: extra }) : shown;
  return excludeName
    ? i18next.t("scales.meta.alsoAskedBy", { list })
    : i18next.t("scales.meta.askedBy", { list });
}

/** One unmatched request: what it is (or a graceful fallback when unnamed), its type, when it
 *  was asked for or arrived, and who asked. Static: there is nothing to open. */
function Row({ u, excludeName }: { u: UnmatchedRequest; excludeName?: string | undefined }) {
  const { t } = useTranslation();
  const kind =
    u.media_type === "movie" ? t("scales.mediaKind.movie") : t("scales.mediaKind.series");
  // Some stored titles already end in their year. Don't print it twice.
  const showYear = u.title != null && u.year != null && !u.title.trim().endsWith(`(${u.year})`);

  const meta: string[] = [kind];
  if (u.available_at) meta.push(t("scales.meta.arrived", { date: date(u.available_at) }));
  else if (u.requested_at) meta.push(t("scales.meta.asked", { date: date(u.requested_at) }));
  const who = requesterLabel(u.requested_by, excludeName);
  if (who) meta.push(who);

  return (
    <div className="scales-title static nis-row">
      <span className="scales-poster" aria-hidden="true">
        <PosterFallback />
      </span>
      <span className="scales-title-main">
        <span className={`scales-title-name${u.title ? "" : " nis-noname"}`}>
          {u.title ?? t("scales.unmatchedList.nameUnavailable")}
          {showYear && <span className="scales-title-yr"> ({u.year})</span>}
          {u.is_4k && <span className="scales-4k">{t("scales.mediaKind.tag4k")}</span>}
        </span>
        <span className="scales-title-meta">
          {meta.map((m, i) => (
            <span key={i}>
              {/* Decorative separator, not a word: a screen reader would read it out as
                  "middle dot" between two facts a listener is trying to hear as a list, so
                  it carries aria-hidden. ScalesPanel has the same separator, hidden the
                  same way. */}
              {i > 0 && (
                <span className="scales-dot" aria-hidden="true">
                  ·
                </span>
              )}
              {m}
            </span>
          ))}
        </span>
      </span>
    </div>
  );
}

function Group({
  heading,
  cls,
  why,
  items,
  excludeName,
}: {
  heading: string;
  cls: string;
  why: string;
  items: UnmatchedRequest[];
  excludeName?: string | undefined;
}) {
  const { t } = useTranslation();
  return (
    <section className={`nis-group ${cls}`}>
      <div className="nis-group-head">
        <h3>{heading}</h3>
        <span className="nis-count">
          {t("scales.unmatchedList.titleCount", { n: items.length })}
        </span>
      </div>
      <p className="nis-why">{why}</p>
      <div className="scales-titles">
        {items.map((u, i) => (
          <Row key={i} u={u} excludeName={excludeName} />
        ))}
      </div>
    </section>
  );
}

/** The grouped list itself, without any panel chrome, so it can sit in the board's own panel
 *  or inside a person's drawer. `excludeName` drops that person from the "asked by" lines. */
export function UnmatchedList({
  items,
  excludeName,
}: {
  items: UnmatchedRequest[];
  excludeName?: string | undefined;
}) {
  const { t } = useTranslation();
  // Any reason the frontend doesn't recognize (a future backend code) still lists under a
  // catch-all, so a request is never silently dropped from the count the card promised.
  const other = items.filter((u) => !KNOWN.has(u.reason));

  return (
    <>
      {REASONS.map(({ key, cls }) => {
        const group = items.filter((u) => u.reason === key);
        if (group.length === 0) return null;
        return (
          <Group
            key={key}
            heading={reasonHeading(key)}
            cls={cls}
            why={reasonWhy(key)}
            items={group}
            excludeName={excludeName}
          />
        );
      })}
      {other.length > 0 && (
        <Group
          heading={t("scales.unmatchedList.otherHeading")}
          cls="nis-group--nomatch"
          why={t("scales.unmatchedList.otherWhy")}
          items={other}
          excludeName={excludeName}
        />
      )}
    </>
  );
}
