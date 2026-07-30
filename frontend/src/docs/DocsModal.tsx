// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs center. It is a wide ModalShell (so it inherits the app's one focus trap,
// scrim, Escape and dialog semantics) split into an index and a reading pane. The index is
// built from the registry, so a new doc appears here with no change to this file.

import { useEffect, useMemo, useRef } from "react";
import { announce } from "../announce";
import { ModalShell } from "../components/ModalShell";
import { docSections } from "./blocks";
import { DocBody } from "./DocBody";
import { DOCS, getDoc, groupedDocs } from "./registry";

export function DocsModal({
  docId,
  anchor,
  nonce,
  onClose,
  onNavigate,
}: {
  docId: string;
  anchor: string | undefined;
  /** Bumped on every openDoc, so re-opening the same doc and anchor still re-scrolls. */
  nonce: number;
  onClose: () => void;
  onNavigate: (id: string, anchor?: string) => void;
}) {
  // DOCS is a non-empty constant, so the fallback always resolves to a real doc.
  const doc = getDoc(docId) ?? DOCS[0]!;
  const contentRef = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => groupedDocs(), []);
  const sections = useMemo(() => docSections(doc), [doc]);

  // Land the reading pane where the caller asked: a section anchor, or the top of the doc.
  // Instant, not smooth: the app scrolls instantly everywhere else because smooth silently
  // no-ops in some environments, and a jump that always lands beats an animation that
  // sometimes doesn't happen.
  useEffect(() => {
    const pane = contentRef.current;
    if (!pane) return;
    // Attribute selector (not CSS.escape) and scrollIntoView (not scrollTo) so this is safe
    // in the test DOM as well as the browser; the anchors are simple kebab-case ids.
    if (anchor) {
      const target = pane.querySelector<HTMLElement>(`[id="${anchor}"]`);
      if (target) {
        target.scrollIntoView({ block: "start" });
        return;
      }
    }
    pane.scrollTop = 0;
  }, [docId, anchor, nonce]);

  // Picking a different doc swaps the whole article while focus stays on the index button that
  // swapped it, so nothing moved and nothing spoke. The pane carries the doc's title as its
  // accessible name (below), but a name that changes on an element the operator is not standing
  // on is announced nowhere -- the index and the pane are siblings, and only one of them had
  // the news (#177). So say which doc is in the pane now.
  //
  // Edge-triggered on the doc, and seeded with the one it opened on: opening the modal is the
  // operator's own press and `ModalShell` already moves focus into the dialog, so announcing
  // there would talk over it. An anchor jump inside one doc is deliberately silent too -- the
  // article is the same article, and the scroll is the answer to the press.
  const saidDocRef = useRef(docId);
  useEffect(() => {
    if (docId === saidDocRef.current) return;
    saidDocRef.current = docId;
    announce(`Showing ${doc.title}.`);
  }, [docId, doc.title]);

  return (
    <ModalShell title={"Help & docs"} onClose={onClose} className="docs-modal">
      <div className="docs-body">
        <nav className="docs-index" aria-label="Documentation">
          {groups.map((g) => (
            <div className="docs-index-group" key={g.group}>
              <p className="docs-index-h">{g.group}</p>
              {g.docs.map((d) => {
                const active = d.id === doc.id;
                return (
                  <div key={d.id}>
                    <button
                      type="button"
                      className={active ? "docs-index-item active" : "docs-index-item"}
                      // Reserve the bold (active) width so selecting an entry never shifts it.
                      data-label={d.title}
                      aria-current={active ? "page" : undefined}
                      onClick={() => onNavigate(d.id)}
                    >
                      {d.title}
                      <small>{d.summary}</small>
                    </button>
                    {active && sections.length > 0 && (
                      <ul className="docs-index-sections">
                        {sections.map((s) => (
                          <li key={s.id}>
                            <button type="button" onClick={() => onNavigate(d.id, s.id)}>
                              {s.text}
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
          <p className="docs-index-foot">More guides are added here as questions come up.</p>
        </nav>

        {/* `overflow-y: auto` with nothing focusable inside it is a pane a keyboard operator
            cannot scroll at all -- a doc is prose, so there is no link or button to tab onto
            and carry the scroll with (WCAG 2.1.1, #177). `tabIndex={0}` makes the pane itself
            a stop, and it carries a role and a name to be worth stopping on: naming it for the
            doc on screen is also what tells the operator the pane changed when they pick a
            different one from the index beside it. */}
        <div
          className="docs-content"
          ref={contentRef}
          tabIndex={0}
          role="region"
          aria-label={doc.title}
        >
          <article>
            <p className="doc-kicker">{doc.group}</p>
            <h1>{doc.title}</h1>
            <p className="doc-summary">{doc.summary}</p>
            <DocBody blocks={doc.body} />
          </article>
        </div>
      </div>
    </ModalShell>
  );
}
