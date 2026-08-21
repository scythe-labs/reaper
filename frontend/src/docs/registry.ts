// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs index. To add a page: write a content file, import it, and add it to DOCS. Its
// group decides which heading it files under; GROUP_ORDER decides the order those headings
// appear. That is the whole extension surface for in-app help.
//
// DOCS is the English manual, and English is the one locale imported statically: the manual
// site is generated from it, and the modal falls back to it entire when no manual ships in
// the operator's language (localized.ts). A translated manual is a directory beside the
// content files, never a second list here.

import type { Doc, DocGroup } from "./blocks";
import { arming } from "./content/arming";
import { cheatSheet } from "./content/cheatSheet";
import { deletionSafety } from "./content/deletionSafety";
import { overview } from "./content/overview";
import { plexRebuild } from "./content/plexRebuild";
import { understandingPolicy } from "./content/understandingPolicy";

export const DOCS: Doc[] = [
  overview,
  understandingPolicy,
  cheatSheet,
  deletionSafety,
  arming,
  plexRebuild,
];

/** The order groups appear in the index. A group not listed here falls to the end, in the
 *  order its first doc appears in DOCS. */
export const GROUP_ORDER: DocGroup[] = ["Getting started", "Policy", "Safety", "Operating"];

/** The doc the generic Help affordance opens when no specific one is asked for. */
export const DEFAULT_DOC = "understanding-policy";

/** The doc with this id, in the manual given: English unless the caller is holding a
 *  translated one. Ids are the same in every locale (manual.locales.test.ts). */
export function getDoc(id: string, docs: Doc[] = DOCS): Doc | undefined {
  return docs.find((d) => d.id === id);
}

/** A manual grouped for the index, groups in GROUP_ORDER then any leftovers, docs in list order. */
export function groupedDocs(docs: Doc[] = DOCS): { group: DocGroup; docs: Doc[] }[] {
  const byGroup = new Map<DocGroup, Doc[]>();
  for (const doc of docs) {
    const list = byGroup.get(doc.group);
    if (list) list.push(doc);
    else byGroup.set(doc.group, [doc]);
  }
  const ordered = [...byGroup.keys()].sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a);
    const ib = GROUP_ORDER.indexOf(b);
    return (ia === -1 ? Infinity : ia) - (ib === -1 ? Infinity : ib);
  });
  return ordered.map((group) => ({ group, docs: byGroup.get(group) as Doc[] }));
}
