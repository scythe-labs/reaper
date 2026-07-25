// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The brand mark worn flat, with no shell: the header and the setup wizard. The figure rides
// `currentColor` so the caller's CSS supplies it (`.brand-mark` sets --text), the cowl opening
// is left TRANSPARENT so the page shows through it, and the eyes keep --accent. That is the
// whole reason this exists alongside BrandBadge: dropping the full ink tile into the masthead
// reads as a sticker in light mode and disappears into the page in dark mode, while the badge
// form is right on the login screen, where the app is introducing itself and has room.
//
// The shell-less form cannot just paint the ink shapes -- they would cover the page instead of
// cutting through the figure. So the ink parts (the band below the shoulders, the face cavity)
// become holes in a luminance mask, and the eyes are painted on top, inside the hole. The mask
// id is scoped with useId so several marks can share a page.
//
// Geometry is the one brand mark (./dissolve); the paint order is the same as the icon's, and
// the comment there explains why the blocks come in two passes.
import { useId } from "react";

import {
  DISSOLVE_BLOCKS_LOWER,
  DISSOLVE_BLOCKS_UPPER,
  DISSOLVE_CUT,
  DISSOLVE_EYE_LEFT_D,
  DISSOLVE_EYE_RIGHT_D,
  DISSOLVE_FACE_D,
  DISSOLVE_HOOD_D,
  DISSOLVE_VIEWBOX,
  type DissolveBlock,
} from "./dissolve";

/** Inside the mask, "show" is white and "cut" is black -- luminance, not the mark's colors. */
const SHOW = "#fff";
const CUT = "#000";

function MaskBlocks({ blocks }: { blocks: readonly DissolveBlock[] }) {
  return (
    <>
      {blocks.map(([x, y, s]) => (
        <rect key={`${x}-${y}-${s}`} x={x} y={y} width={s} height={s} fill={SHOW} />
      ))}
    </>
  );
}

export function BrandMark({
  className,
  size,
  title,
}: {
  className?: string;
  size?: number;
  /** Omitted by default: in the masthead and the wizard the word "Reaper" sits right beside
   *  the mark, so announcing it again is noise. Pass a title only where it stands alone. */
  title?: string;
}) {
  const mask = `${useId()}-m`;
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox={DISSOLVE_VIEWBOX}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <mask id={mask} maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">
        <rect width="64" height="64" fill={CUT} />
        <path d={DISSOLVE_HOOD_D} fill={SHOW} />
        <rect
          x={DISSOLVE_CUT.x}
          y={DISSOLVE_CUT.y}
          width={DISSOLVE_CUT.width}
          height={DISSOLVE_CUT.height}
          fill={CUT}
        />
        <MaskBlocks blocks={DISSOLVE_BLOCKS_UPPER} />
        <path d={DISSOLVE_FACE_D} fill={CUT} />
        <MaskBlocks blocks={DISSOLVE_BLOCKS_LOWER} />
      </mask>
      <rect width="64" height="64" fill="currentColor" mask={`url(#${mask})`} />
      <path d={DISSOLVE_EYE_LEFT_D} fill="var(--accent)" />
      <path d={DISSOLVE_EYE_RIGHT_D} fill="var(--accent)" />
    </svg>
  );
}
