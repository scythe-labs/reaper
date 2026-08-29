// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every manual the build ships, for the gates that read them all: English first, then each
// translated one by tag. Eager, so it never runs in the app. `docs/localized.ts` holds the
// lazy twin over the same pattern, and `manual.locales.test.ts` pins that the two agree: two
// walks over one population, checked against each other.

import type { Doc } from "../docs/blocks";
import { ENGLISH, type Manual } from "../docs/localized";

const shipped = import.meta.glob<{ DOCS: Doc[] }>("../docs/content/*/index.ts", {
  eager: true,
});

export const MANUALS: Manual[] = [
  ENGLISH,
  ...Object.entries(shipped)
    .map(([path, m]) => ({ lng: path.split("/").at(-2) as string, docs: m.DOCS }))
    .sort((a, b) => a.lng.localeCompare(b.lng)),
];
