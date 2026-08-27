// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The app icon as an in-app badge (the login screen). Same drawing as the favicon (appIcon.ts),
// but rendered in JSX so the eyes follow --accent live, with no JS: the ink shell and the bone
// figure are fixed, the eyes are the accent. The geometry is the one brand mark (./dissolve).
// The clip path id is scoped with useId so more than one badge can share a page without id
// collisions.
import { useId } from "react";

import { APP_ICON_RADIUS } from "./appIcon";
import {
  DISSOLVE_BONE,
  DISSOLVE_EYE_LEFT_D,
  DISSOLVE_EYE_RIGHT_D,
  DISSOLVE_INK,
  DISSOLVE_VIEWBOX,
} from "./dissolve";
import { DISSOLVE_FIGURE_D } from "./dissolve.generated";

export function BrandBadge({
  size = 64,
  className,
  title = "Reaper",
}: {
  size?: number;
  className?: string;
  title?: string;
}) {
  const clip = `${useId()}-r`;
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox={DISSOLVE_VIEWBOX}
      role="img"
      aria-label={title}
    >
      <clipPath id={clip}>
        <rect width="64" height="64" rx={APP_ICON_RADIUS} />
      </clipPath>
      <rect width="64" height="64" rx={APP_ICON_RADIUS} fill={DISSOLVE_INK} />
      <g clipPath={`url(#${clip})`}>
        {/* The finished shape rather than the recipe (./dissolve.generated), so this draws the
            same geometry BrandMark does. The cowl opening is a notch in that one contour, and
            the ink tile behind shows through it as the face cavity. See BrandMark for why this
            geometry is composed ahead of render time, as one path with no fill rule. */}
        <path d={DISSOLVE_FIGURE_D} fill={DISSOLVE_BONE} />
        <path d={DISSOLVE_EYE_LEFT_D} fill="var(--accent)" />
        <path d={DISSOLVE_EYE_RIGHT_D} fill="var(--accent)" />
      </g>
    </svg>
  );
}
