// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The "not in the last scan" list: every available request the scan didn't judge, grouped by
// WHY, so the operator can see what each one is and why it wasn't matched. Shared by two
// surfaces -- the board's own panel (NotInScanPanel, opened from the amber tile) and each
// person's drawer (ScalesPanel, where it replaces the old "N not listed here" footer) -- so
// both read the same way. It decides nothing: like the rest of Scales, it explains.
//
// A row never opens anything (there is no candidate to open), so it rests as static text,
// reusing the requested-title row chrome. Titles are looked up server-side; when one is
// missing the row falls back to its type and date, never an id (rule 21).

import type { UnmatchedRequest } from "../api";
import { count, date } from "../format";
import { PosterFallback } from "./ScalesPanel";

/** The reason groups, in reading order: the benign-and-expected first (a rescan fixes it),
 *  then the ones worth a look. Each names itself and carries one bound line explaining what
 *  it means for their files (rule 45). A colored left stripe encodes the reason at a glance. */
const REASONS: { key: string; heading: string; cls: string; why: string }[] = [
  {
    key: "after_scan",
    heading: "Added since the last scan",
    cls: "nis-group--new",
    why: "These arrived or were requested after your last scan ran. Your next scan will include them.",
  },
  {
    key: "set_aside",
    heading: "On your server, but set aside",
    cls: "nis-group--aside",
    why: "The last scan didn't weigh these. Usually your keep rules protect the whole show, it isn't fully downloaded, or the server holding it was offline during the scan.",
  },
  {
    key: "no_id",
    heading: "Couldn't be matched",
    cls: "nis-group--nomatch",
    why: "Seerr sent no movie or show id for these, so they can't be lined up with anything on your server.",
  },
];

const KNOWN = new Set(REASONS.map((r) => r.key));

/** Who asked, in plain words. On the board it names the requesters ("by A, B"); inside one
 *  person's drawer their own name is dropped and it reads "also asked by X" -- matching the
 *  in-scan rows, so a shared title never looks like it was theirs alone. Long lists trail off
 *  as "and N others" rather than running off the row. */
function requesterLabel(names: string[], excludeName?: string): string | null {
  const others = excludeName ? names.filter((n) => n !== excludeName) : names;
  if (others.length === 0) return null;
  const shown = others.slice(0, 2).join(", ");
  const extra = others.length - 2;
  const list =
    extra > 0 ? `${shown} and ${count(extra)} ${extra === 1 ? "other" : "others"}` : shown;
  return excludeName ? `also asked by ${list}` : `by ${list}`;
}

/** One unmatched request: what it is (or a graceful fallback when unnamed), its type, when it
 *  was asked for or arrived, and who asked. Static -- there is nothing to open. */
function Row({ u, excludeName }: { u: UnmatchedRequest; excludeName?: string | undefined }) {
  const kind = u.media_type === "movie" ? "Movie" : "Series";
  // Some stored titles already end in their year; don't print it twice.
  const showYear =
    u.title != null && u.year != null && !u.title.trim().endsWith(`(${u.year})`);

  const meta: string[] = [kind];
  if (u.available_at) meta.push(`arrived ${date(u.available_at)}`);
  else if (u.requested_at) meta.push(`asked ${date(u.requested_at)}`);
  const who = requesterLabel(u.requested_by, excludeName);
  if (who) meta.push(who);

  return (
    <div className="scales-title static nis-row">
      <span className="scales-poster" aria-hidden="true">
        <PosterFallback />
      </span>
      <span className="scales-title-main">
        <span className={`scales-title-name${u.title ? "" : " nis-noname"}`}>
          {u.title ?? "Name unavailable"}
          {showYear && <span className="scales-title-yr"> ({u.year})</span>}
          {u.is_4k && <span className="scales-4k">4K</span>}
        </span>
        <span className="scales-title-meta">
          {meta.map((m, i) => (
            <span key={i}>
              {i > 0 && <span className="scales-dot">·</span>}
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
  return (
    <section className={`nis-group ${cls}`}>
      <div className="nis-group-head">
        <h3>{heading}</h3>
        <span className="nis-count">
          {count(items.length)} {items.length === 1 ? "title" : "titles"}
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
  // Any reason the frontend doesn't recognize (a future backend code) still lists under a
  // catch-all, so a request is never silently dropped from the count the card promised.
  const other = items.filter((u) => !KNOWN.has(u.reason));

  return (
    <>
      {REASONS.map(({ key, heading, cls, why }) => {
        const group = items.filter((u) => u.reason === key);
        if (group.length === 0) return null;
        return (
          <Group
            key={key}
            heading={heading}
            cls={cls}
            why={why}
            items={group}
            excludeName={excludeName}
          />
        );
      })}
      {other.length > 0 && (
        <Group
          heading="Not in the last scan"
          cls="nis-group--nomatch"
          why="These requests aren't in the last scan. Run a new scan to sort them out."
          items={other}
          excludeName={excludeName}
        />
      )}
    </>
  );
}
