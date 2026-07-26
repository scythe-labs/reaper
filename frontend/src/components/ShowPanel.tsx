// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The show's own information panel: what opens when you click a show card.
//
// A season's why-panel answers "why this season?"; this answers the question above it,
// "where does the whole show stand?" -- every season across every lane, side by side,
// plus the one sentence that put the show in front of you (a keep-rule conflict's full
// wording lives here, under the show it belongs to, not on the card). Clicking a season
// hands off to that season's complete reasoning.

import type { Candidate, Group } from "../api";
import { itemBytes, totalBytes } from "../format";
import { useOverrideMutations } from "../useOverrideMutations";
import { LibraryChip, OverrideControls, ShowStatusChip } from "./ReviewQueue";
import { handFate, showReapIsNoop } from "./reviewFate";
import { chipWhy, CondemnedChip, OverrideChip, StatusChip } from "./StatusChip";
import { JumpPill, Synopsis, WhyClose, WhyHero } from "./WhyPanel";

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
  onOpenSeason: (id: number) => void;
  onClose: () => void;
}) {
  const seasonLabel = group.seasons.length === 1 ? "1 season" : `${group.seasons.length} seasons`;

  // The show panel is where a whole-show call gets made, so Spare and Reap live at its bottom
  // edge too, through the same shared hook the cards and the why-panel use so every view
  // refreshes together. The decision covers every season; the control toggles the show's OWN
  // key, so it reads the show's own decision (show_override), never an aggregate of the
  // seasons' own marks -- clearing this key cannot clear those, so lighting it from them was a
  // dead toggle. A season overridden on its own keeps its mark in the strip and its row.
  const { setOverride, clearOverride } = useOverrideMutations();
  const showOverride = group.show_override;

  return (
    <aside className="why">
      <WhyClose onClose={onClose} />
      {group.poster_url && <WhyHero posterUrl={group.poster_url} />}

      <header className="why-head">
        <div>
          <h2>
            {group.links.plex ? (
              <a
                className="title-link"
                href={group.links.plex}
                target="_blank"
                rel="noopener noreferrer"
                title="Open in Plex"
              >
                {group.title}
                {group.year && <span className="card-year"> {group.year}</span>}
              </a>
            ) : (
              <>
                {group.title}
                {group.year && <span className="card-year"> {group.year}</span>}
              </>
            )}
          </h2>
          <p className="muted why-sub">
            TV show &middot; {seasonLabel} &middot;{" "}
            {totalBytes(group.size_bytes, group.unknown_size_seasons)}
            {/* The Plex library the show lives in -- the same quiet chip the card carries. */}
            <LibraryChip library={group.library} />
            {/* Named here as well as on the card: the card is where you notice a show has
                ended, this is where you came to find out. A panel that dropped the status
                the card just showed would read as a contradiction. */}
            <ShowStatusChip status={group.show_status} />
            <JumpPill href={group.links.sonarr} label="Sonarr" />
            <JumpPill href={group.links.tautulli} label="Tautulli" />
            <JumpPill href={group.links.seerr} label="Seerr" />
          </p>
        </div>
      </header>

      {group.summary && <Synopsis text={group.summary} />}

      <StatusChip chip={group.chip} />
      {/* The full sentence behind the chip -- a keep-rule conflict's complete wording,
          or whatever line put this show in front of you. */}
      {group.reason && <p className="show-reason">{group.reason}</p>}

      <section className="block">
        <h3>Seasons</h3>
        <p className="blurb">
          Every season on disk, whatever Reaper decided. Click one for its full reasoning.
        </p>
        <ul className="panel-seasons">
          {group.seasons.map((season) => (
            <li key={season.id}>
              <button
                type="button"
                className="panel-season"
                onClick={() => onOpenSeason(season.id)}
              >
                <span className={`score score-${handFate(season)}`}>{season.score}</span>
                <span className="panel-season-name">
                  {season.season_number === 0
                    ? "Specials"
                    : season.season_number != null
                      ? `Season ${season.season_number}`
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
            <span className="error">Couldn't save that. Try again.</span>
          )}
        </div>
      </div>
    </aside>
  );
}
