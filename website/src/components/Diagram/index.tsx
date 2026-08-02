// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A safety flow, drawn from the app's own vocabulary rather than a diagramming library. It is a
// vertical spine of nodes joined by labeled connectors, where a decision can shed one side
// branch. Shape says what a step is (decision, terminal), tone says how it ends in Reaper's
// verdict language: `keep` the file survives, `stop` the run halts. Endpoints stay neutral.
//
// No Mermaid, for the same reason `DocBody` draws it by hand: every node here is real, selectable
// text that re-themes with the page, which a rendered image is not. The container scrolls
// sideways on a narrow screen rather than clipping, so nothing is unreachable on a phone.

type Tone = "keep" | "stop";
type Shape = "process" | "decision" | "terminal";

type Node = { text: string; sub?: string; shape?: Shape; tone?: Tone };
type Enter = { label?: string; phase?: string };
type Branch = { label?: string; node: Node };
type Step = { node: Node; enter?: Enter; branch?: Branch };

export type DiagramSpec = {
  title?: string;
  legend?: { tone: Tone; text: string }[];
  steps: Step[];
};

function NodeBox({ node }: { node: Node }) {
  const cls = ["rp-dg__node"];
  if (node.shape === "decision") cls.push("rp-dg__node--decision");
  else if (node.shape === "terminal") cls.push("rp-dg__node--term");
  if (node.tone) cls.push(`rp-dg__node--${node.tone}`);
  return (
    <div className={cls.join(" ")}>
      {node.text}
      {node.sub && <span className="rp-dg__sub">{node.sub}</span>}
    </div>
  );
}

export function Diagram({ spec }: { spec: DiagramSpec }) {
  return (
    <figure className="rp-dg">
      {spec.title && <figcaption className="rp-dg__title">{spec.title}</figcaption>}
      <div className="rp-dg__scroll">
        {spec.steps.map((step, i) => (
          <div className="rp-dg__step" key={i}>
            {step.enter?.phase && <div className="rp-dg__phase">{step.enter.phase}</div>}
            {/* The first step is entered from nowhere, so it draws no connector above it. */}
            {i > 0 && (
              <div className="rp-dg__arrow">
                <span className="rp-dg__line" aria-hidden="true" />
                {step.enter?.label && <span className="rp-dg__label">{step.enter.label}</span>}
              </div>
            )}
            <div className="rp-dg__row">
              <NodeBox node={step.node} />
              {step.branch && (
                <div className="rp-dg__branch">
                  <span className="rp-dg__branch-line" aria-hidden="true" />
                  {step.branch.label && (
                    <span className="rp-dg__label">{step.branch.label}</span>
                  )}
                  <NodeBox node={step.branch.node} />
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      {spec.legend && spec.legend.length > 0 && (
        <ul className="rp-dg__legend">
          {spec.legend.map((entry) => (
            <li key={entry.tone} className={`rp-dg__key rp-dg__key--${entry.tone}`}>
              {entry.text}
            </li>
          ))}
        </ul>
      )}
    </figure>
  );
}
