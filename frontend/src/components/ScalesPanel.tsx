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
import { useTranslation } from "react-i18next";
import type { PersonDetail, PersonTitle, QuotaLine, Verdict } from "../api";
import { bytes, carriesYear, count, date, itemBytes, titleWithYear } from "../format";
import i18next from "../i18n";
import { BalanceBar } from "./BalanceBar";
import { PosterFallback } from "./PosterFallback";
import { ExternalMark } from "./queueIcons";
import { UnmatchedList } from "./UnmatchedList";
import { type WatchReach, reachIsMeasured, reachNote, watchReach } from "./watchReach";
import { PanelFallback, WhyShell } from "./WhyShell";

function initial(name: string): string {
  const c = name.trim()[0];
  return c ? c.toUpperCase() : "?";
}

/** The title's poster, proxied from Plex through /api/poster, with the film-strip mark as a
 *  fallback when there is no poster key or the image cannot load. */
function Poster({ url }: { url: string | null }) {
  // No reset effect, unlike the queue's poster and the two backdrops (`useArtFallback`). The
  // row key this sits under carries the title's id when it has one, so a different title is
  // a different component and the flag starts false. That key falls back to a display title,
  // which rule 19 forbids, so the first branch is what holds this up. Change it and the flag
  // latches: one failed load leaves the film strip on every title the row is reused for.
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
  if (line.limit === null) return i18next.t("scales.personPanel.limits.unlimited");
  // A daily quota read "1 per 1 days" while this same file and Fairness already say
  // "person"/"people" and "title"/"titles" (U-19).
  return i18next.t("scales.personPanel.limits.text", {
    limit: count(line.limit),
    hasDays: line.days ? "yes" : "no",
    days: line.days ?? 0,
  });
}

function LimitChip({ label, line }: { label: string; line: QuotaLine }) {
  const { t } = useTranslation();
  return (
    <span className={`scales-limit${line.at_limit ? " at" : ""}`}>
      <span className="scales-limit-k">{label}</span>
      <span className="scales-limit-v">
        {line.limit === null ? (
          <span className="scales-limit-sub">{t("scales.personPanel.limits.unlimited")}</span>
        ) : (
          limitText(line)
        )}
        {line.at_limit && t("scales.personPanel.limits.atLimit")}
      </span>
    </span>
  );
}

/** The person's name in the panel head. Links to their page on the request portal when one
 *  could be built ({base_url}/users/{id}); plain text otherwise, never a dead link. Follows
 *  the app's title-link idiom: text at rest, an accent underline on hover, a small outbound
 *  arrow so the link is discoverable. */
function ProfileName({ id, name, href }: { id: string; name: string; href: string | null }) {
  const { t } = useTranslation();
  if (!href) return <h2 id={id}>{name}</h2>;
  return (
    <a
      className="scales-name-link"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={t("scales.personPanel.openInPortal")}
    >
      <h2 id={id}>{name}</h2>
      <ExternalMark className="scales-ext" />
    </a>
  );
}

/** The fate marker: the loud, actionable states wear a colored chip; "kept" is the quiet,
 *  expected state, so it reads as a plain gray label rather than a green pill on every row. */
function Fate({ verdict }: { verdict: string }) {
  const { t } = useTranslation();
  if (verdict === "condemn")
    return (
      <span className="status-chip status-pressure">
        {t("scales.personPanel.fate.reclaimable")}
      </span>
    );
  if (verdict === "abstain")
    return (
      <span className="status-chip status-look">{t("scales.personPanel.fate.leftToDecide")}</span>
    );
  if (verdict === "protect")
    return <span className="scales-kept">{t("scales.personPanel.fate.kept")}</span>;
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
  if (!reachIsMeasured(reach)) return i18next.t("scales.personPanel.watched.noHistory");
  if (t.watched_by_them <= 0)
    return i18next.t("scales.personPanel.watched.noneSince", { date: date(reach.since) });
  // "times", not a multiplication sign. The glyph carries the meaning here rather than
  // decorating it, and a reader at its default symbol level drops it -- leaving "watched 3" on
  // the screen where the operator decides whose files to delete. It is inside a composed string,
  // so there is no element to hang `aria-hidden` on even if hiding it were right: the word is
  // the fix, exactly as the middots in composed strings became commas (#299, rule 21).
  if (t.media_type === "movie") {
    return i18next.t("scales.personPanel.watched.movieTimes", { n: t.watched_by_them });
  }
  return i18next.t("scales.personPanel.watched.episodes", { n: t.watched_by_them });
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
  // Aliased: the prop above is already named `t` (the title), so the translate function takes
  // the alias `tr` rather than shadowing it.
  const { t: tr } = useTranslation();
  const kind =
    t.media_type === "movie" ? tr("scales.mediaKind.movie") : tr("scales.mediaKind.series");
  // Some stored titles already carry their year (e.g. "Some Show (2019)"); don't print it
  // twice. Only append the year when the title does not already end with it. The search term
  // the jump below carries asks the same question through the same helper, so a title that
  // names its own year is not handed a second one there either.
  const showYear = t.year != null && !carriesYear(t.title, t.year);

  const meta: string[] = [kind];
  if (t.requested_at) meta.push(tr("scales.meta.asked", { date: date(t.requested_at) }));
  if (t.available_at) meta.push(tr("scales.meta.arrived", { date: date(t.available_at) }));
  if (t.co_requesters.length > 0)
    meta.push(tr("scales.meta.alsoAskedBy", { list: t.co_requesters.join(", ") }));

  const body = (
    <>
      <Poster url={t.poster_url} />
      <span className="scales-title-main">
        <span className="scales-title-name">
          {t.title}
          {showYear && <span className="scales-title-yr"> ({t.year})</span>}
          {t.is_4k && <span className="scales-4k">{tr("scales.mediaKind.tag4k")}</span>}
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
    <button
      type="button"
      className="scales-title"
      title={tr("scales.personPanel.openInReview")}
      onClick={onOpen}
    >
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
  const { t } = useTranslation();
  const headingId = useId();
  const granted = detail.gb_granted_bytes;
  const reclaim = detail.reclaimable_bytes;
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
              {t("scales.personPanel.requestsSummary", {
                n: count(detail.requests_in_scan),
                hasMore: detail.not_in_scan > 0 ? "yes" : "other",
                m: count(detail.not_in_scan),
              })}
            </p>
          </div>
        </div>
      </div>

      <section className="block">
        <h3>{t("scales.personPanel.balanceHeading")}</h3>
        <BalanceBar granted={granted} reclaim={reclaim} hasReclaim={hasReclaim} />
        <div className="scales-tiles">
          <div className="fair-stat">
            <span className="fair-stat-num">{bytes(granted)}</span>
            <span className="fair-stat-lbl">{t("scales.personPanel.grantedLabel")}</span>
            <span className="fair-stat-sub">{t("scales.personPanel.grantedSub")}</span>
          </div>
          <div className="fair-stat">
            {watched === null ? (
              <>
                <span className="fair-stat-num muted">{t("scales.personPanel.unknown")}</span>
                <span className="fair-stat-lbl">{t("scales.personPanel.theyWatchedLabel")}</span>
                <span className="fair-stat-sub">
                  {reach.kind === "no_account"
                    ? t("scales.personPanel.noAccountSub")
                    : t("scales.personPanel.noHistorySub")}
                </span>
              </>
            ) : (
              <>
                <span className={`fair-stat-num ${watched >= 50 ? "green" : "red"}`}>
                  {watched}%
                </span>
                <span className="fair-stat-lbl">{t("scales.personPanel.theyWatchedLabel")}</span>
                <span className="fair-stat-sub">{t("scales.personPanel.ofWhatTheyAskedFor")}</span>
              </>
            )}
          </div>
          <div className="fair-stat">
            <span className="fair-stat-num">{count(detail.requests_in_scan)}</span>
            <span className="fair-stat-lbl">{t("scales.personPanel.requestsLabel")}</span>
            <span className="fair-stat-sub">{t("scales.personPanel.stillInScan")}</span>
          </div>
          <div className="fair-stat">
            {hasReclaim ? (
              <>
                <span className="fair-stat-num red">{bytes(reclaim)}</span>
                <span className="fair-stat-lbl">{t("scales.personPanel.reclaimableLabel")}</span>
                <span className="fair-stat-sub red">
                  {t("scales.personPanel.reclaimableCount", { n: detail.reclaimable_items })}
                </span>
              </>
            ) : (
              <>
                <span className="fair-stat-num green">{t("scales.personPanel.none")}</span>
                <span className="fair-stat-lbl">{t("scales.personPanel.reclaimableLabel")}</span>
                <span className="fair-stat-sub">{t("scales.personPanel.allEarningKeep")}</span>
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
          <h3>{t("scales.personPanel.limitsHeading")}</h3>
          <div className="scales-limits">
            {detail.quota && (
              <LimitChip
                label={t("scales.personPanel.limits.moviesLabel")}
                line={detail.quota.movie}
              />
            )}
            {detail.quota && (
              <LimitChip
                label={t("scales.personPanel.limits.seriesLabel")}
                line={detail.quota.tv}
              />
            )}
            {detail.seerr_total !== null && (
              <span className="scales-limit">
                <span className="scales-limit-k">
                  {t("scales.personPanel.limits.lifetimeLabel")}
                </span>
                <span className="scales-limit-v">
                  {t("scales.personPanel.limits.lifetimeRequests", {
                    n: count(detail.seerr_total),
                  })}
                </span>
              </span>
            )}
          </div>
        </section>
      )}

      <section className="block">
        <div className="scales-h3row">
          <h3>{t("scales.personPanel.allRequestsHeading")}</h3>
          <span className="scales-count">
            {t("scales.personPanel.titlesCount", {
              n: detail.titles.length,
              hasMore: detail.not_in_scan > 0 ? "yes" : "other",
              m: count(detail.not_in_scan),
            })}
          </span>
        </div>
        {detail.titles.length === 0 ? (
          <p className="scales-foot">{t("scales.personPanel.noTitles")}</p>
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
            <h3>{t("scales.personPanel.notInScanHeading")}</h3>
            <span className="scales-count">
              {t("scales.personPanel.notInScanCount", { n: detail.not_in_scan })}
            </span>
          </div>
          <UnmatchedList items={detail.unmatched} excludeName={detail.name} />
        </section>
      )}
    </WhyShell>
  );
}

/** What the panel's column shows while the breakdown is loading, or when it could not be
 *  loaded. `PanelFallback` in `WhyShell.tsx` is the whole of it, `WhyPanelFallback` its twin,
 *  and these three strings all that differ. */
export function ScalesPanelFallback({ error, onClose }: { error: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <PanelFallback
      error={error}
      onClose={onClose}
      waiting={t("scales.personPanel.fallback.waiting")}
      loading={t("scales.personPanel.fallback.loading")}
      failure={t("scales.personPanel.fallback.failure")}
    />
  );
}
