// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs index. To add a page: write a content file, import it, and add it to DOCS. Its
// group decides which heading it files under; GROUP_ORDER decides the order those headings
// appear. That is the whole extension surface for in-app help.

import type { Doc } from "./blocks";
import { arming } from "./content/arming";
import { cheatSheet } from "./content/cheatSheet";
import { overview } from "./content/overview";
import { understandingPolicy } from "./content/understandingPolicy";

export const DOCS: Doc[] = [overview, understandingPolicy, cheatSheet, arming];

/** The order groups appear in the index. A group not listed here falls to the end, in the
 *  order its first doc appears in DOCS. */
export const GROUP_ORDER = ["Getting started", "Policy", "Safety"];

/** The doc the generic Help affordance opens when no specific one is asked for. */
export const DEFAULT_DOC = "understanding-policy";

export function getDoc(id: string): Doc | undefined {
  return DOCS.find((d) => d.id === id);
}

/** DOCS grouped for the index, groups in GROUP_ORDER then any leftovers, docs in DOCS order. */
export function groupedDocs(): { group: string; docs: Doc[] }[] {
  const byGroup = new Map<string, Doc[]>();
  for (const doc of DOCS) {
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
