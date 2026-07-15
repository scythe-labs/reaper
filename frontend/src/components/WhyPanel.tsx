// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The why-panel. This is Reaper's reason to exist.
//
// Every competitor can tell you *which rules matched*. None of them show the work. The
// three blocks below are, in order of how much trust they buy:
//
//   1. The signals that fired, with their actual contributions.
//   2. The protections that were checked and did NOT fire -- with the real numbers.
//      "checked: popular here -- 0 watchers in the last 365 days, your floor is 3".
//      This is the block that makes a verdict auditable rather than merely asserted.
//   3. The protections that could not be checked at all, rendered *differently*.
//      "We could not look" is not "we looked and it was fine". Every tool that renders
//      them alike eventually deletes something during an API outage.
//
// And it renders for PROTECTED items too, showing the score it is overriding. A tool
// that only explains its deletions cannot be trusted about its keeps.

import { useEffect, useRef, useState } from "react";
import type { CandidateDetail, GateOutcome } from "../api";
import { bytes, coverage, since } from "../format";

/** The synopsis, clamped to two lines with a "more" to expand. The card shows the *reason*
 *  now, so this slide-out is the one place the plot lives -- but it still should not push the
 *  reasoning below the fold, hence the clamp. */
function Synopsis({ text }: { text: string }) {
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

/** The backdrop banner at the top of the panel: the item's wide Plex art, fading down into
 *  the panel so the reasoning below reads on a plain surface. Falls back to the poster when
 *  a title has no separate art, and to nothing at all when it has neither. */
function WhyHero({ posterUrl }: { posterUrl: string }) {
  const [src, setSrc] = useState(`${posterUrl}?kind=art`);
  const fellBack = useRef(false);

  // When the selected item changes without an unmount in between (the new item's detail was
  // already cached, so `detail` goes A -> B directly), this component is reused rather than
  // remounted. Reset the art to the new item's, or the hero keeps showing the previous
  // item's backdrop under the new title/score -- exactly the kind of mismatch this panel
  // exists to avoid. Mirrors ReviewQueue's Backdrop.
  useEffect(() => {
    fellBack.current = false;
    setSrc(`${posterUrl}?kind=art`);
  }, [posterUrl]);

  if (!src) return null;
  return (
    <div className="why-hero">
      <img
        src={src}
        alt=""
        aria-hidden="true"
        onError={() => {
          if (!fellBack.current) {
            fellBack.current = true;
            setSrc(posterUrl); // no wide art -> the poster still fills the banner
          } else {
            setSrc(""); // neither -> drop the banner
          }
        }}
      />
      <div className="why-hero-fade" aria-hidden="true" />
    </div>
  );
}

function Verdict({ item }: { item: CandidateDetail }) {
  const { verdict, score, explanation } = item;

  return (
    <div className={`verdict verdict-${verdict}`}>
      <div className="verdict-label">{verdict}</div>
      <div className="verdict-score">
        <strong>{score}</strong>
        <span className="muted">/100 &middot; your threshold is {explanation.threshold}</span>
      </div>

      {verdict === "protect" && (
        <p className="verdict-note">
          Something is protecting this, so <strong>the score doesn't matter</strong> &mdash; it's
          kept no matter what, and nothing can change that.
        </p>
      )}
      {verdict === "abstain" && (
        <p className="verdict-note">
          Reaper is not confident enough to judge this one. It scored below your threshold, or
          too little of it could be seen &mdash; either way, abstaining keeps the file.
        </p>
      )}
    </div>
  );
}

/** One signal's contribution, as a bar you can compare against the others.
 *
 *  The denominator is the signal's *weight*, not the total, so a 70-weight signal
 *  contributing 70 fills the bar. That makes "this signal is maxed out" legible at a
 *  glance, which is the question you actually ask when tuning. */
function Signal({ signal }: { signal: CandidateDetail["explanation"]["signals"][number] }) {
  const filled = signal.weight > 0 ? (signal.contribution / signal.weight) * 100 : 0;

  return (
    <li className={signal.evaluated ? "signal" : "signal signal-unknown"}>
      <div className="signal-head">
        <span className="signal-amount">
          {signal.evaluated ? `+${signal.contribution.toFixed(1)}` : "—"}
          <span className="muted">/{signal.weight}</span>
        </span>
        <span className="signal-detail">{signal.detail}</span>
      </div>
      <div className="bar">
        <div className="bar-fill" style={{ width: `${Math.min(filled, 100)}%` }} />
      </div>
      {!signal.evaluated && (
        <p className="signal-note">
          Reaper couldn't check this one, so it added nothing — which can only pull the score{" "}
          <em>down</em>, never up.
        </p>
      )}
    </li>
  );
}

function Gates({
  title,
  blurb,
  outcomes,
  tone,
}: {
  title: string;
  blurb: string;
  outcomes: GateOutcome[];
  tone: "fired" | "checked" | "unknown";
}) {
  if (outcomes.length === 0) return null;

  const mark = tone === "fired" ? "✓" : tone === "checked" ? "✓" : "?";

  return (
    <section className="block">
      <h3>{title}</h3>
      <p className="blurb">{blurb}</p>
      <ul className={`gates gates-${tone}`}>
        {outcomes.map((outcome) => (
          <li key={outcome.gate}>
            <span className="gate-mark">{mark}</span>
            <span className="gate-detail">{outcome.detail}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function WhyPanel({ item, onClose }: { item: CandidateDetail; onClose: () => void }) {
  const { explanation } = item;

  const mediaLabel = item.media_type === "season" ? "TV season" : item.media_type;

  return (
    <aside className="why">
      {item.poster_url && <WhyHero posterUrl={item.poster_url} />}

      <header className="why-head">
        <div>
          <h2>
            {item.title}
            {item.year && <span className="card-year"> {item.year}</span>}
          </h2>
          <p className="muted">
            {bytes(item.size_bytes)} &middot; {mediaLabel} &middot;{" "}
            <code>{item.media_key}</code>
          </p>
        </div>
        <button className="ghost why-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>

      {item.summary && <Synopsis text={item.summary} />}

      <Verdict item={item} />

      {item.first_flagged_at && (
        <p className="flagged">
          On the list since {since(item.first_flagged_at)}. It waits out a grace period —
          which you can cancel — before Reaper can remove it.
        </p>
      )}

      <section className="block">
        <h3>Why it scored {explanation.score}</h3>
        <p className="blurb">
          Reasons to believe nobody will watch it again. Reaper saw{" "}
          <strong>{coverage(item.coverage_bp)}</strong> of the evidence it looks for.
        </p>
        <ul className="signals">
          {explanation.signals.map((signal) => (
            <Signal key={signal.id} signal={signal} />
          ))}
        </ul>
      </section>

      <Gates
        title="Protections that fired"
        blurb="Any one of these keeps the file, whatever it scored."
        outcomes={explanation.protections_fired}
        tone="fired"
      />

      <Gates
        title="Protections that were checked — and did not fire"
        blurb="What Reaper looked for and did not find. The numbers are the ones it actually used."
        outcomes={explanation.protections_checked}
        tone="checked"
      />

      <Gates
        title="Protections that could not be checked"
        blurb={
          "Reaper could not look — a source was unreachable or the data was missing. This is " +
          "not the same as looking and finding nothing, so it is never treated as evidence " +
          "for deleting. It can only ever protect."
        }
        outcomes={explanation.protections_unknown}
        tone="unknown"
      />
    </aside>
  );
}
