// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one place a doc's blocks become React. Every visual choice (callout tones, table
// emphasis, the verdict colors) lives here and reuses the app's tokens, so a doc reads as
// part of Reaper. Add a block variant in blocks.ts, then a case here; nowhere else.

import { createContext, Fragment, type ReactNode, useContext } from "react";
import { useTranslation } from "react-i18next";
import type { Block, CalloutTone, DiagramBlock, DiagramNode } from "./blocks";

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]\n]+\]\([a-z0-9-]+(?:#[a-z0-9-]+)?\))/g;
/** `[text](doc-id#section)`. The target is a doc id, never a URL, so a cross-reference can
 *  only ever point inside the docs; `manual.links.test.ts` fails on an id or section that
 *  does not exist, which is what keeps the two surfaces from shipping a dead link. */
const REF = /^\[([^\]]+)\]\(([a-z0-9-]+)(?:#([a-z0-9-]+))?\)$/;

/** How a cross-reference opens, supplied by whatever is rendering the doc. The docs modal
 *  passes its own navigator so a link moves within the open modal; anything rendering a doc
 *  without one gets the link text as plain prose rather than a button that does nothing. */
const NavCtx = createContext<((id: string, anchor?: string) => void) | null>(null);

function DocRef({ doc, anchor, text }: { doc: string; anchor?: string | undefined; text: string }) {
  const navigate = useContext(NavCtx);
  if (navigate === null) return <>{text}</>;
  return (
    <button type="button" className="doc-ref" onClick={() => navigate(doc, anchor)}>
      {text}
    </button>
  );
}

/** Parse the tiny inline subset (`**bold**`, `` `code` ``, `[text](doc-id#section)`) into
 *  React nodes. Never HTML: it builds elements, so a doc string cannot inject markup. */
export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (let m = INLINE.exec(text); m !== null; m = INLINE.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(<strong key={key++}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("[")) {
      // INLINE only matches the shape REF parses, so the destructure always lands. Rendering
      // the raw token on the impossible branch keeps a future edit to either pattern visible
      // as text rather than as a link that silently goes nowhere.
      const [, label, target, anchor] = REF.exec(tok) ?? [];
      if (label === undefined || target === undefined) out.push(tok);
      else out.push(<DocRef key={key++} doc={target} anchor={anchor} text={label} />);
    } else out.push(<code key={key++}>{tok.slice(1, -1)}</code>);
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function CalloutIcon({ tone }: { tone: CalloutTone }) {
  const common = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2.2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (tone === "tip") {
    return (
      <svg {...common}>
        <path d="m9 12 2 2 4-4" />
        <circle cx="12" cy="12" r="9" />
      </svg>
    );
  }
  if (tone === "caution") {
    return (
      <svg {...common}>
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
        <path d="M12 9v4M12 17h.01" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
  );
}

/** One flow node. Its class list encodes shape (decision pill, terminal stadium) and tone
 *  (keep = green, stop = red), so the same node styling serves the spine and the branches. */
function DiagramNodeBox({ node }: { node: DiagramNode }) {
  const cls = ["dd-node"];
  if (node.shape === "decision") cls.push("dd-decision");
  else if (node.shape === "terminal") cls.push("dd-term");
  if (node.tone) cls.push(`dd-${node.tone}`);
  return (
    <div className={cls.join(" ")}>
      {node.text}
      {node.sub && <span className="dd-sub">{node.sub}</span>}
    </div>
  );
}

/** A hand-drawn flowchart (no Mermaid dependency): a centered spine of nodes joined by
 *  labeled connectors, each decision able to shed a side branch. Every node is real, readable
 *  text; the container scrolls sideways on a narrow pane rather than clipping. */
function DocDiagram({ block }: { block: DiagramBlock }) {
  const { t } = useTranslation();
  return (
    // Named but unreachable was the worse half of this: the group advertised itself in the
    // accessibility tree as somewhere to go, and the Tab order never went there, so the
    // sideways scroll a narrow pane needs could not be moved from a keyboard (WCAG 2.1.1,
    // #177). `.doc-table` below and the four outside this file went the same way (rule 72).
    <div
      className="doc-diagram"
      tabIndex={0}
      role="group"
      aria-label={
        block.title
          ? t("shell.docBody.flowchartWithTitle", { title: block.title })
          : t("shell.docBody.flowchart")
      }
    >
      {block.title && <p className="doc-diagram-cap">{block.title}</p>}
      {block.legend && block.legend.length > 0 && (
        <div className="doc-diagram-legend">
          {block.legend.map((l, i) => (
            <span key={i}>
              <span className={`dd-sw dd-${l.tone}`} />
              {l.text}
            </span>
          ))}
        </div>
      )}
      <div className="dd-flow">
        {block.steps.map((s, i) => (
          <Fragment key={i}>
            {i > 0 && (
              <>
                {s.enter?.phase && <div className="dd-phase">{s.enter.phase}</div>}
                <div className="dd-drop">
                  {s.enter?.label && <span className="dd-elabel">{s.enter.label}</span>}
                  <span className="dd-vline" />
                  <span className="dd-varrow" />
                </div>
              </>
            )}
            <div className="dd-row">
              <div className="dd-spine">
                <DiagramNodeBox node={s.node} />
              </div>
              {s.branch && (
                <div
                  className={
                    s.branch.node.tone ? `dd-branch dd-${s.branch.node.tone}` : "dd-branch"
                  }
                >
                  <span className="dd-hline" />
                  <span className="dd-harrow" />
                  {s.branch.label && <span className="dd-elabel">{s.branch.label}</span>}
                  <DiagramNodeBox node={s.branch.node} />
                </div>
              )}
            </div>
          </Fragment>
        ))}
      </div>
    </div>
  );
}

function renderBlock(b: Block, key: number): ReactNode {
  switch (b.kind) {
    case "h":
      // `h4`/`h5`, not `h2`/`h3`: the article hangs off the doc title `DocsModal` renders at
      // `h3`, which itself sits under the `h2` `ModalShell` titles every dialog with. At the
      // old levels a section of a doc read as a SIBLING of the whole dialog rather than as
      // part of the doc it is in (#177). The `sub` flag, not the tag, is what decides a jump
      // target, so the index is unaffected.
      return b.sub ? (
        <h5 key={key} id={b.id}>
          {b.text}
        </h5>
      ) : (
        <h4 key={key} id={b.id}>
          {b.text}
        </h4>
      );
    case "p":
      return <p key={key}>{inline(b.text)}</p>;
    case "callout":
      return (
        <div key={key} className={`doc-callout ${b.tone}`}>
          <span className="doc-callout-ic">
            <CalloutIcon tone={b.tone} />
          </span>
          <p>{inline(b.text)}</p>
        </div>
      );
    case "list": {
      const items = b.items.map((it, i) => <li key={i}>{inline(it)}</li>);
      return b.ordered ? (
        <ol key={key} className="doc-list">
          {items}
        </ol>
      ) : (
        <ul key={key} className="doc-list">
          {items}
        </ul>
      );
    }
    case "steps":
      return (
        <ol key={key} className="doc-steps">
          {b.items.map((s, i) => (
            <li key={i}>
              <b>{s.title}</b>
              <span>{inline(s.text)}</span>
            </li>
          ))}
        </ol>
      );
    case "table":
      return (
        // Horizontal scroll with no focusable cell, so a keyboard could not move it (WCAG
        // 2.1.1, #177). Focusable and nothing more, unlike the five siblings: a table block
        // carries no title, so `role="region"` here could only be an UNNAMED landmark, which
        // is worse than none -- it adds a stop to the landmark list that says nothing about
        // what it holds. A reader entering the stop reads the table, which is the name.
        <div key={key} className="doc-table" tabIndex={0}>
          <table>
            <thead>
              <tr>
                {b.head.map((cell, i) => (
                  <th key={i} scope="col">
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {b.rows.map((row, ri) => (
                <tr key={ri}>
                  {row.map((cell, ci) => (
                    <td key={ci} className={ci === b.hi ? "hi" : undefined}>
                      {inline(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "diagram":
      return <DocDiagram key={key} block={b} />;
  }
}

export function DocBody({
  blocks,
  onNavigate,
}: {
  blocks: Block[];
  onNavigate?: (id: string, anchor?: string) => void;
}) {
  return (
    <NavCtx.Provider value={onNavigate ?? null}>
      {blocks.map((b, i) => renderBlock(b, i))}
    </NavCtx.Provider>
  );
}
