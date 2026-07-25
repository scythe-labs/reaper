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
  DISSOLVE_BLOCKS_LOWER,
  DISSOLVE_BLOCKS_UPPER,
  DISSOLVE_BONE,
  DISSOLVE_CUT,
  DISSOLVE_EYE_LEFT_D,
  DISSOLVE_EYE_RIGHT_D,
  DISSOLVE_FACE_D,
  DISSOLVE_HOOD_D,
  DISSOLVE_INK,
  DISSOLVE_VIEWBOX,
  type DissolveBlock,
} from "./dissolve";

function Blocks({ blocks }: { blocks: readonly DissolveBlock[] }) {
  return (
    <>
      {blocks.map(([x, y, s]) => (
        <rect key={`${x}-${y}-${s}`} x={x} y={y} width={s} height={s} fill={DISSOLVE_BONE} />
      ))}
    </>
  );
}

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
        <path d={DISSOLVE_HOOD_D} fill={DISSOLVE_BONE} />
        <rect
          x={DISSOLVE_CUT.x}
          y={DISSOLVE_CUT.y}
          width={DISSOLVE_CUT.width}
          height={DISSOLVE_CUT.height}
          fill={DISSOLVE_INK}
        />
        <Blocks blocks={DISSOLVE_BLOCKS_UPPER} />
        <path d={DISSOLVE_FACE_D} fill={DISSOLVE_INK} />
        <path d={DISSOLVE_EYE_LEFT_D} fill="var(--accent)" />
        <path d={DISSOLVE_EYE_RIGHT_D} fill="var(--accent)" />
        <Blocks blocks={DISSOLVE_BLOCKS_LOWER} />
      </g>
    </svg>
  );
}
