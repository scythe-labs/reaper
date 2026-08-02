// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A glossary. A real <dl> so the term-to-definition relationship is in the markup: the policy
// glossary is the page an operator reaches while deciding what a word on the Reap screen means,
// and a list of styled paragraphs would not let them jump between terms.

import type { ReactNode } from "react";

export function Definitions({ children }: { children: ReactNode }) {
  return <dl className="rp-defs">{children}</dl>;
}

export function Def({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="rp-defs__term">{term}</dt>
      <dd className="rp-defs__body">{children}</dd>
    </>
  );
}
