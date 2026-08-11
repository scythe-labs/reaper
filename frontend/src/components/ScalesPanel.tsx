// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Scales person panel: one requester's whole request story, opened from a card. It is
// the review screen's why-panel shell (`.why`, `main.split`) reused wholesale, so it rides
// the same responsive behavior -- a side column on wide screens, a right-hand sheet that
// slides over the list below 1100px, full-screen under 900px -- and the same head/close
// grammar. A person is not a title, so there is no poster hero and no whole-person
// Spare/Reap footer: this panel decides nothing, it explains. Each title row still opens
// its real card in Review, exactly like the old reclaimable chips did.

import { useId, useState } from "react";
import { useSlowWait } from "../announce";
import type { PersonDetail, PersonTitle, QuotaLine, Verdict } from "../api";
import { bytes, carriesYear, count, date, itemBytes, titleWithYear } from "../format";
import { PosterFallback } from "./PosterFallback";
import { UnmatchedList } from "./UnmatchedList";
import { type WatchReach, reachIsMeasured, reachNote, watchReach } from "./watchReach";
import { WhyShell } from "./WhyShell";
import { Notice } from "./Notice";

function initial(name: string): string {
  const c = name.trim()[0];
  return c ? c.toUpperCase() : "?";
}

/** The title's poster, proxied from Plex through /api/poster, with the film-strip mark as a
 *  fallback when there is no poster key or the image cannot load. */
function Poster({ url }: { url: string | null }) {
  // No reset effect, unlike the queue's poster and the two backdrops (`useArtFallback`). The
  // row key this sits under carries the title's own id, so a different title is a different
  // component and the flag starts false. Change that key and the flag latches: one failed
  // load would leave the film strip on every title the row is reused for (rule 19).
  const [failed, setFailed] = useState(false);
  return (
    <span className="scales-poster" aria-hidden="true">
      {url && !failed ? (
        <img src={url} alt="" loading="lazy" onError={() => setFailed(true)} />
      ) : (
        <PosterFallback />
      )}
    </span>
  );
}

/** A media type's request limit, in plain words: "1 per 14 days", "unlimited", and an amber
 *  "at limit" flag when they are capped there right now. */
function limitText(line: QuotaLine): string {
  if (line.limit === null) return "unlimited";
  // A daily quota read "1 per 1 days" while this same file and Fairness already say
  // "person"/"people" and "title"/"titles" (U-19).
  const per = line.days ? (line.days === 1 ? " per day" : ` per ${count(line.days)} days`) : "";
  return `${count(line.limit)}${per}`;
}

function LimitChip({ label, line }: { label: string; line: QuotaLine }) {
  return (
    <span className={`scales-limit${line.at_limit ? " at" : ""}`}>
      <span className="scales-limit-k">{label}</span>
      <span className="scales-limit-v">
        {line.limit === null ? (
          <span className="scales-limit-sub">unlimited</span>
        ) : (
          limitText(line)
        )}
        {line.at_limit && ", at limit"}
      </span>
    </span>
  );
}

/** The person's name in the panel head. Links to their page on the request portal when one
 *  could be built ({base_url}/users/{id}); plain text otherwise, never a dead link. Follows
 *  the app's title-link idiom: text at rest, an accent underline on hover, a small outbound
 *  arrow so the link is discoverable. */
function ProfileName({ id, name, href }: { id: string; name: string; href: string | null }) {
  if (!href) return <h2 id={id}>{name}</h2>;
  return (
    <a
      className="scales-name-link"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title="Open this person in the request portal"
    >
      <h2 id={id}>{name}</h2>
      <svg
        className="scales-ext"
        viewBox="0 0 16 16"
        width="13"
        height="13"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="M6 3h7v7M13 3L4 12"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </a>
  );
}

/** The fate marker: the loud, actionable states wear a colored chip; "kept" is the quiet,
 *  expected state, so it reads as a plain gray label rather than a green pill on every row. */
function Fate({ verdict }: { verdict: string }) {
  if (verdict === "condemn")
    return <span className="status-chip status-pressure">Reclaimable</span>;
  if (verdict === "abstain") return <span className="status-chip status-look">Left to decide</span>;
  if (verdict === "protect") return <span className="scales-kept">Kept</span>;
  return <span className="scales-kept">{verdict}</span>;
}

/** How much of it they watched, in the item's own terms: a movie's plays, a series' distinct
 *  episodes watched. Never a raw play sum for a show, which reads as inflated next to Tautulli.
 *
 *  The negative branch is the careful one. `watched_by_them` is counted over the whole mirror
 *  and the mirror begins at its horizon, so a zero is a LOWER BOUND: it says nothing about
 *  plays behind that date, which on an install whose history is younger than its library is
 *  most of them. A bare "not watched" states a verified never as fact on the screen where the
 *  operator decides whose files to delete, so a zero is only ever printed with the date it
 *  counts from, and where nothing is readable at all (`reachIsMeasured`, covering both an
 *  unlinked request account and an empty mirror) it says so instead of naming a number. */
function watchedLabel(t: PersonTitle, reach: WatchReach): string {
  if (!reachIsMeasured(reach)) return "can't see their history";
  if (t.watched_by_them <= 0) return `none since ${date(reach.since)}`;
  // "times", not a multiplication sign. The glyph carries the meaning here rather than
  // decorating it, and a reader at its default symbol level drops it -- leaving "watched 3" on
  // the screen where the operator decides whose files to delete. It is inside a composed string,
  // so there is no element to hang `aria-hidden` on even if hiding it were right: the word is
  // the fix, exactly as the middots in composed strings became commas (#299, rule 21).
  if (t.media_type === "movie") {
    return `watched ${count(t.watched_by_them)} ${t.watched_by_them === 1 ? "time" : "times"}`;
  }
  const n = count(t.watched_by_them);
  return `${n} ${t.watched_by_them === 1 ? "episode" : "episodes"} watched`;
}

/** One requested title: what it is, when it arrived, whether they watched it, its fate, and
 *  who else asked. The whole row opens that item's real card in Review (a movie or lone
 *  season by id, a whole show by its group), so the reasoning is one click away. */
function TitleRow({
  t,
  onOpen,
  reach,
}: {
  t: PersonTitle;
  onOpen: (() => void) | null;
  reach: WatchReach;
}) {
  const kind = t.media_type === "movie" ? "Movie" : "Series";
  // Some stored titles already carry their year (e.g. "Some Show (2019)"); don't print it
  // twice. Only append the year when the title does not already end with it. The search term
  // the jump below carries asks the same question through the same helper, so a title that
  // names its own year is not handed a second one there either.
  const showYear = t.year != null && !carriesYear(t.title, t.year);

  const meta: string[] = [kind];
  if (t.requested_at) meta.push(`asked ${date(t.requested_at)}`);
  if (t.available_at) meta.push(`arrived ${date(t.available_at)}`);
  if (t.co_requesters.length > 0) meta.push(`also asked by ${t.co_requesters.join(", ")}`);

  const body = (
    <>
      <Poster url={t.poster_url} />
      <span className="scales-title-main">
        <span className="scales-title-name">
          {t.title}
          {showYear && <span className="scales-title-yr"> ({t.year})</span>}
          {t.is_4k && <span className="scales-4k">4K</span>}
        </span>
        <span className="scales-title-meta">
          {meta.map((m, i) => (
            <span key={i}>
              {/* A separator, not a word: read out it lands as "middle dot" between two
                  facts a reader is trying to hear as a list (#177). Its twin in
                  UnmatchedList/ScalesPanel carries the same hide (rule 72). */}
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
      <span className="scales-title-side">
        <Fate verdict={t.verdict} />
        <span className="scales-size">{itemBytes(t.size_bytes)}</span>
        <span
          className={`scales-watch ${reachIsMeasured(reach) && t.watched_by_them > 0 ? "yes" : "no"}`}
        >
          {watchedLabel(t, reach)}
        </span>
      </span>
    </>
  );

  if (!onOpen) return <div className="scales-title static">{body}</div>;
  return (
    <button type="button" className="scales-title" title="Open this in Review" onClick={onOpen}>
      {body}
    </button>
  );
}

/** The panel proper. Takes an already-fetched detail (App owns the query, exactly as it does
 *  for the why-panel), so this stays testable without standing up React Query. */
export function ScalesPanel({
  detail,
  onClose,
  onOpenItem,
  onOpenGroup,
}: {
  detail: PersonDetail;
  onClose: () => void;
  /** Open one title in Review, on the lane it lives in, with the queue searched down to it. */
  onOpenItem: (candidateId: number, lane: Verdict, search: string) => void;
  onOpenGroup: (key: string, lane: Verdict, search: string) => void;
}) {
  const headingId = useId();
  const granted = detail.gb_granted_bytes;
  const reclaim = detail.reclaimable_bytes;
  const used = Math.max(0, granted - reclaim);
  const usedPct = granted > 0 ? (100 * used) / granted : 100;
  const reclaimPct = granted > 0 ? (100 * reclaim) / granted : 0;
  // How far Reaper can see into this person's watching, from the one derivation the board's
  // cards also read (rule 72). Null where there is no reading to report -- no Plex account
  // behind the request account, or an empty mirror -- because `played_by_them` is then
  // structurally 0 (`fairness._roll_up` counts plays only inside `if pid is not None`) and a
  // red 0% would be a measurement nobody took. The rows below take the same `reach`, and the
  // note under the tiles bounds the percentage that IS shown.
  const reach = watchReach(detail.plex_id, detail.horizon_at);
  const note = reachNote(reach);
  const watched = !reachIsMeasured(reach)
    ? null
    : detail.requests_in_scan > 0
      ? Math.round((100 * detail.played_by_them) / detail.requests_in_scan)
      : 0;
  const hasReclaim = detail.reclaimable_items > 0;

  // The lane travels with the jump. Review's queue is one lane of three, and this row already
  // knows which one the title is in -- it is the fate printed beside it, and the server derives
  // it the same override-aware way the queue filters (rule 77). Sending only the id landed the
  // panel over whichever lane the operator happened to leave the queue on, so a "Left to decide"
  // title opened its reasoning above a Condemned list it is not in, with nothing on screen
  // saying where to find it.
  // The title travels with it too, into the queue's search box. The lane alone can still be
  // thousands of rows deep, and the opened panel says nothing about where in that list its card
  // is; seeding the search leaves one title on screen beside its own reasoning, and the chip
  // above the list says what was searched and clears in one click. The year goes with the title
  // because the row prints it -- and because the search now understands one (`list_candidates`).
  const opener = (t: PersonTitle): (() => void) | null => {
    const term = titleWithYear(t.title, t.year);
    if (t.item_id != null) return () => onOpenItem(t.item_id as number, t.verdict, term);
    if (t.group_key != null) return () => onOpenGroup(t.group_key as string, t.verdict, term);
    return null;
  };

  const showLimits = detail.quota !== null || detail.seerr_total !== null;

  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      <div className="why-head">
        <div className="scales-head-id">
          <span className="fair-avatar" aria-hidden="true">
            {initial(detail.name)}
          </span>
          <div>
            <ProfileName id={headingId} name={detail.name} href={detail.profile_url} />
            <p className="why-sub muted">
              {count(detail.requests_in_scan)} requests in the last scan
              {detail.not_in_scan > 0 && `, ${count(detail.not_in_scan)} not in it`}
            </p>
          </div>
        </div>
      </div>

      <section className="block">
        <h3>The balance</h3>
        <div
          className="fair-balance"
          role="img"
          aria-label={
            hasReclaim
              ? `${bytes(used)} kept, ${bytes(reclaim)} the scan would reclaim`
              : `${bytes(used)} kept, nothing reclaimable`
          }
        >
          <i className="used" style={{ width: `${usedPct}%` }} />
          {reclaimPct > 0 && <i className="reclaim" style={{ width: `${reclaimPct}%` }} />}
        </div>
        <div className="scales-tiles">
          <div className="fair-stat">
            <span className="fair-stat-num">{bytes(granted)}</span>
            <span className="fair-stat-lbl">Granted</span>
            <span className="fair-stat-sub">disk they asked for</span>
          </div>
          <div className="fair-stat">
            {watched === null ? (
              <>
                <span className="fair-stat-num muted">Unknown</span>
                <span className="fair-stat-lbl">They watched</span>
                <span className="fair-stat-sub">
                  {reach.kind === "no_account"
                    ? "no Plex account, so no history to read"
                    : "no watch history to read yet"}
                </span>
              </>
            ) : (
              <>
                <span className={`fair-stat-num ${watched >= 50 ? "green" : "red"}`}>
                  {watched}%
                </span>
                <span className="fair-stat-lbl">They watched</span>
                <span className="fair-stat-sub">of what they asked for</span>
              </>
            )}
          </div>
          <div className="fair-stat">
            <span className="fair-stat-num">{count(detail.requests_in_scan)}</span>
            <span className="fair-stat-lbl">Requests</span>
            <span className="fair-stat-sub">still in the scan</span>
          </div>
          <div className="fair-stat">
            {hasReclaim ? (
              <>
                <span className="fair-stat-num red">{bytes(reclaim)}</span>
                <span className="fair-stat-lbl">Reclaimable</span>
                <span className="fair-stat-sub red">
                  {count(detail.reclaimable_items)}{" "}
                  {detail.reclaimable_items === 1 ? "title" : "titles"}
                </span>
              </>
            ) : (
              <>
                <span className="fair-stat-num green">None</span>
                <span className="fair-stat-lbl">Reclaimable</span>
                <span className="fair-stat-sub">all still earning their keep</span>
              </>
            )}
          </div>
        </div>
        {/* The span every figure above and every row below is counted over. The board renders
            the same line, but on a phone this panel is a sheet OVER the board (`main.split
            .why`), so the board's copy is not on screen to borrow. */}
        {note && <p className="fair-horizon muted">{note}</p>}
      </section>

      {showLimits && (
        <section className="block">
          <h3>Request limits</h3>
          <div className="scales-limits">
            {detail.quota && <LimitChip label="Movies" line={detail.quota.movie} />}
            {detail.quota && <LimitChip label="Series" line={detail.quota.tv} />}
            {detail.seerr_total !== null && (
              <span className="scales-limit">
                <span className="scales-limit-k">Lifetime</span>
                <span className="scales-limit-v">{count(detail.seerr_total)} requests</span>
              </span>
            )}
          </div>
        </section>
      )}

      <section className="block">
        <div className="scales-h3row">
          <h3>Everything they asked for</h3>
          <span className="scales-count">
            {count(detail.titles.length)} {detail.titles.length === 1 ? "title" : "titles"}
            {detail.not_in_scan > 0 && `, ${count(detail.not_in_scan)} not in the scan`}
          </span>
        </div>
        {detail.titles.length === 0 ? (
          <p className="scales-foot">
            None of their requests are in the last scan yet, so there is nothing to list.
          </p>
        ) : (
          <div className="scales-titles">
            {detail.titles.map((t, i) => (
              <TitleRow
                key={`${t.item_id ?? t.group_key ?? t.title}-${i}`}
                t={t}
                onOpen={opener(t)}
                reach={reach}
              />
            ))}
          </div>
        )}
      </section>

      {detail.unmatched.length > 0 && (
        <section className="block">
          <div className="scales-h3row">
            <h3>Not in the last scan</h3>
            <span className="scales-count">
              {count(detail.not_in_scan)} {detail.not_in_scan === 1 ? "request" : "requests"}
            </span>
          </div>
          <UnmatchedList items={detail.unmatched} excludeName={detail.name} />
        </section>
      )}
    </WhyShell>
  );
}

/** What the panel's column shows while the breakdown is loading, or when it could not be
 *  loaded. The column is reserved the moment a person is picked, so a blank would read as a
 *  hang; and it keeps its own close, or a failed fetch would strand the reader in split view.
 *  Mirrors the why-panel's fallback. */
export function ScalesPanelFallback({ error, onClose }: { error: boolean; onClose: () => void }) {
  const headingId = useId();
  // Null on the failure arm, which reaches `Notice`'s `role="alert"` and speaks for itself.
  // Mirrors `WhyPanelFallback` (rule 72).
  useSlowWait(error ? null : "Still gathering this person's requests.");
  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      {error ? (
        <>
          <div className="why-head">
            <h2 id={headingId}>Something went wrong</h2>
          </div>
          <Notice tone="error">
            Couldn't load this person's requests. Close this panel and click the card to try again.
          </Notice>
        </>
      ) : (
        // Live region dropped, sentence moved to `announce.tsx` (#332), as in `WhyPanelFallback`.
        <div className="why-loading">
          <span className="spinner spinner-lg" aria-hidden="true" />
          {/* The loading branch has no heading to point at, so the lead carries the name. A
              panel named "Gathering their requests…" is what is true at that moment. */}
          <p className="why-loading-lead" id={headingId}>
            Gathering their requests…
          </p>
        </div>
      )}
    </WhyShell>
  );
}
