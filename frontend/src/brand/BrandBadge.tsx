// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Deep app icon as an in-app badge (the login screen). Same drawing as the favicon
// (deepIcon.ts), but rendered in JSX so the platter follows --accent live through color-mix,
// with no JS: the dark shell is fixed, the disk = accent (+10% white / x0.55), the glow =
// accent. The scythe geometry is the one brand mark (./scythe). Gradient ids are scoped with
// useId so more than one badge can share a page without id collisions.
import { useId } from "react";

import { SCYTHE_BLADE_D, SCYTHE_SNATH_D, SCYTHE_SNATH_WIDTH } from "./scythe";

export function BrandBadge({
  size = 64,
  className,
  title = "Reaper",
}: {
  size?: number;
  className?: string;
  title?: string;
}) {
  const uid = useId();
  const shell = `${uid}-s`;
  const glow = `${uid}-g`;
  const disk = `${uid}-d`;
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 512 512"
      role="img"
      aria-label={title}
    >
      <defs>
        <linearGradient id={shell} x1="0.1" y1="0" x2="0.85" y2="1">
          <stop offset="0" stopColor="#0f3040" />
          <stop offset="1" stopColor="#061620" />
        </linearGradient>
        <radialGradient id={glow} cx="0.5" cy="0.46" r="0.5">
          <stop offset="0" style={{ stopColor: "var(--accent)", stopOpacity: 0.55 }} />
          <stop offset="1" style={{ stopColor: "var(--accent)", stopOpacity: 0 }} />
        </radialGradient>
        <linearGradient id={disk} x1="0.15" y1="0" x2="0.85" y2="1">
          <stop offset="0" style={{ stopColor: "color-mix(in srgb, var(--accent), #fff 10%)" }} />
          <stop offset="1" style={{ stopColor: "color-mix(in srgb, var(--accent), #000 45%)" }} />
        </linearGradient>
      </defs>
      <rect width="512" height="512" rx="143" fill={`url(#${shell})`} />
      <circle cx="256" cy="238" r="215" fill={`url(#${glow})`} />
      <circle cx="256" cy="256" r="176" fill={`url(#${disk})`} />
      <circle cx="256" cy="256" r="176" fill="none" stroke="#fff" strokeOpacity="0.25" strokeWidth="3" />
      <g fill="none" stroke="#fff" strokeOpacity="0.28">
        <circle cx="256" cy="256" r="140" strokeWidth="4" />
        <circle cx="256" cy="256" r="100" strokeWidth="4" />
      </g>
      <g transform="translate(256 258) scale(8.2) translate(-22 -24.5)">
        <path d={SCYTHE_BLADE_D} fill="#fff" />
        <path
          d={SCYTHE_SNATH_D}
          fill="none"
          stroke="#fff"
          strokeWidth={SCYTHE_SNATH_WIDTH}
          strokeLinecap="round"
        />
      </g>
    </svg>
  );
}
