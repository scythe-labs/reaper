// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The show's own information panel: what opens when you click a show card.
//
// A season's why-panel answers "why this season?"; this answers the question above it,
// "where does the whole show stand?" -- every season across every lane, side by side,
// plus the one sentence that put the show in front of you (a keep-rule conflict's full
// wording lives here, under the show it belongs to, not on the card). Clicking a season
// hands off to that season's complete reasoning.

import { useState } from "react";
import type { Group } from "../api";
import { bytes } from "../format";
import { CondemnedChip, StatusChip } from "./StatusChip";
import { JumpPill, WhyHero } from "./WhyPanel";

/** The synopsis, clamped like the why-panel's -- the seasons block is the point here. */
function ShowSynopsis({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <p className="why-summary">
      <span className={expanded ? undefined : "clamp-2"}>{text}</span>
      {text.length > 150 && (
        <button className="link-btn" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "less" : "more"}
        </button>
      )}
    </p>
  );
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

  return (
    <aside className="why">
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
            TV show &middot; {seasonLabel} &middot; {bytes(group.size_bytes)}
            <JumpPill href={group.links.sonarr} label="Sonarr" />
            <JumpPill href={group.links.tautulli} label="Tautulli" />
            <JumpPill href={group.links.seerr} label="Seerr" />
          </p>
        </div>
        <button className="ghost why-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>

      {group.summary && <ShowSynopsis text={group.summary} />}

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
              <button type="button" className="panel-season" onClick={() => onOpenSeason(season.id)}>
                <span className={`score score-${season.verdict}`}>{season.score}</span>
                <span className="panel-season-name">
                  {season.season_number === 0
                    ? "Specials"
                    : season.season_number != null
                      ? `Season ${season.season_number}`
                      : season.title}
                </span>
                {season.verdict === "condemn" ? (
                  <CondemnedChip />
                ) : (
                  <StatusChip chip={season.chip} />
                )}
                <span className="panel-season-size num">{bytes(season.size_bytes)}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </aside>
  );
}
