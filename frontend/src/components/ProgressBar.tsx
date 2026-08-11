// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The `.bar` progress track, in one place.
//
// A bare div with an inline width is a picture of a number and nothing else, so each of these
// carries the role and the value. Three components wrote the same eight lines, two of them under
// a comment pointing at the other copies (#177, rule 72), which is a sweep that has to be
// remembered rather than a component that has to be imported.

/** The scan-style progress track: a labeled `progressbar` filling to `percent`.
 *
 *  The label is the whole accessible name, so it says what is running ("Scanning your library"),
 *  not what the element is. `aria-valuetext` is deliberately absent: every caller renders the
 *  phase and the percent as visible text beside the bar, and a valuetext would only repeat it.
 *  The deletion path's two bars are NOT this component: `ReapConfirm` adds a valuetext over
 *  different chrome, and `ReapBar` puts the role on its text because its container holds Stop. */
export function ProgressBar({ label, percent }: { label: string; percent: number }) {
  return (
    <div
      className="bar"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(percent)}
    >
      <div className="bar-fill" style={{ width: `${percent}%` }} />
    </div>
  );
}
