// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs center. It is a wide ModalShell (so it inherits the app's one focus trap,
// scrim, Escape and dialog semantics) split into an index and a reading pane. The index is
// built from the registry, so a new doc appears here with no change to this file.

import { useEffect, useMemo, useRef } from "react";
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

        <div className="docs-content" ref={contentRef}>
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
