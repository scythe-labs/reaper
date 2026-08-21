// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The show's own information panel: what opens when you click a show card.
//
// A season's why-panel answers "why this season?"; this answers the question above it,
// "where does the whole show stand?" -- every season across every lane, side by side,
// plus the one sentence that put the show in front of you (a keep-rule conflict's full
// wording lives here, under the show it belongs to, not on the card). Clicking a season
// hands off to that season's complete reasoning.

import { useId } from "react";
import { useTranslation } from "react-i18next";
import type { Candidate, Group, Verdict } from "../api";
import { itemBytes, totalBytes } from "../format";
import { cardReason } from "../why";
import { useOverrideMutations } from "../useOverrideMutations";
import { LibraryChip, OverrideControls, ShowStatusChip } from "./ReviewQueue";
import { handFate, laneOf, showReapIsNoop } from "./reviewFate";
import { chipWhy, CondemnedChip, OverrideChip, StatusChip } from "./StatusChip";
import { MatchCandidates, PanelHead, Synopsis, WhyHero } from "./WhyPanel";
import { WhyShell } from "./WhyShell";

/** The one pill a season row wears. The owner's hand decision replaces the scan chip
 *  (solid means "you chose this"); a reap the engine can't honor yet reads dashed red and
 *  says why, because "you asked" and "it is gone" are different facts. The wording is the
 *  shared chip's, in this list's own class family. */
function SeasonPill({ season }: { season: Candidate }) {
  if (season.override !== null) {
    return (
      <OverrideChip
        override={season.override}
        effective={season.override_effective}
        keptWhy={chipWhy(season.chip)}
        spareCoversUntil={season.spare_covers_until}
        family="status-chip"
      />
    );
  }
  if (season.verdict === "condemn") return <CondemnedChip />;
  return <StatusChip chip={season.chip} />;
}

export function ShowPanel({
  group,
  onOpenSeason,
  onClose,
}: {
  group: Group;
  /** Opens that season's own reasoning, on the lane it is in. This list spans every lane
   *  (see the blurb over it), so the season the operator picks is often not on the lane the
   *  queue behind this panel is showing, and `laneOf` is what says which one it is. */
  onOpenSeason: (id: number, lane: Verdict) => void;
  onClose: () => void;
}) {
  // The show panel is where a whole-show call gets made, so Spare and Reap live at its bottom
  // edge too, through the same shared hook the cards and the why-panel use so every view
  // refreshes together. The decision covers every season; the control toggles the show's OWN
  // key, so it reads the show's own decision (show_override), never an aggregate of the
  // seasons' own marks -- clearing this key cannot clear those, so lighting it from them was a
  // dead toggle. A season overridden on its own keeps its mark in the strip and its row.
  const { t } = useTranslation();
  const { setOverride, clearOverride } = useOverrideMutations();
  const showOverride = group.show_override;
  const headingId = useId();
  const reason = cardReason(group);

  return (
    <WhyShell headingId={headingId} onClose={onClose}>
      {group.poster_url && <WhyHero posterUrl={group.poster_url} />}

      <PanelHead
        headingId={headingId}
        title={group.title}
        year={group.year}
        links={group.links}
        sub={
          <>
            {t("reviewQueue.showPanel.subtitle", {
              n: group.seasons.length,
              size: totalBytes(group.size_bytes, group.unknown_size_seasons),
            })}
            {/* The Plex library the show lives in -- the same quiet chip the card carries. */}
            <LibraryChip library={group.library} />
            {/* Named here as well as on the card: the card is where you notice a show has
                ended, this is where you came to find out. A panel that dropped the status
                the card just showed would read as a contradiction. */}
            <ShowStatusChip status={group.show_status} />
          </>
        }
      />

      {group.summary && <Synopsis text={group.summary} />}

      <StatusChip chip={group.chip} />
      {/* The full sentence behind the chip -- a keep-rule conflict's complete wording,
          or whatever line put this show in front of you. */}
      {reason && <p className="show-reason">{reason}</p>}
      {/* The Plex rows an abstain could not choose between, directly under the sentence
          naming the problem -- the same pairing the season why-panel uses (rule 72). The
          header's own Plex link above is built from the show's rating key, which is null on
          exactly these rows, so without this the panel says Plex and Sonarr disagree and
          offers nothing to open. Renders nothing on every other show. */}
      <MatchCandidates links={group.links} />

      <section className="block">
        <h3>{t("reviewQueue.showPanel.seasonsHeading")}</h3>
        <p className="blurb">{t("reviewQueue.showPanel.seasonsBlurb")}</p>
        <ul className="panel-seasons">
          {group.seasons.map((season) => (
            <li key={season.id}>
              <button
                type="button"
                className="panel-season"
                onClick={() => onOpenSeason(season.id, laneOf(season))}
              >
                <span className={`score score-${handFate(season)}`}>{season.score}</span>
                {/* Two of these three branches are labels the panel composes, short enough that
                    the row's nowrap costs them nothing. The third is the server's own title, and
                    it is the only one that needs rule 139's break opportunity -- so it says so,
                    rather than the row giving up its steady height for all three. */}
                <span
                  className={`panel-season-name${
                    season.season_number == null ? " is-server-title" : ""
                  }`}
                >
                  {season.season_number === 0
                    ? t("reviewQueue.specials")
                    : season.season_number != null
                      ? t("reviewQueue.seasonNumber", { n: season.season_number })
                      : season.title}
                </span>
                <SeasonPill season={season} />
                <span className="panel-season-size num">{itemBytes(season.size_bytes)}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {/* Decide the whole show without leaving its reasoning, pinned to the panel's bottom
          edge like the movie and single-season panels. Both buttons, because a whole-show
          Reap still takes the seasons the scan kept; it drops only once the whole show is
          condemned (showReapIsNoop), where it would be the no-op a condemned movie's is. */}
      <div className="why-actions">
        <div className="why-actions-row">
          <OverrideControls
            override={showOverride}
            onSet={(decision, spareDays) =>
              setOverride.mutate({ key: group.group_key, decision, spareDays: spareDays ?? 0 })
            }
            onClear={() => clearOverride.mutate(group.group_key)}
            pending={setOverride.isPending || clearOverride.isPending}
            hideReap={showReapIsNoop(group.seasons)}
            spareExpiresAt={group.show_spare_expires_at}
            roomy
          />
          {(setOverride.isError || clearOverride.isError) && (
            <span className="error">{t("reviewQueue.saveError")}</span>
          )}
        </div>
      </div>
    </WhyShell>
  );
}
