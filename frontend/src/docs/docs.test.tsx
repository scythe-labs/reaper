// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs system: the registry stays coherent, the block renderer is faithful, and the
// open-from-anywhere contract opens the right page (and deep-links to a section).

import { render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";
import { GATE_META } from "../components/policyMeta";
import { docSections } from "./blocks";
import { understandingPolicy } from "./content/understandingPolicy";
import { DocBody } from "./DocBody";
import { DocsModal } from "./DocsModal";
import { DocsProvider, useDocs } from "./DocsContext";
import { DOCS, getDoc, groupedDocs, GROUP_ORDER } from "./registry";

describe("registry", () => {
  it("has coherent, uniquely-identified docs", () => {
    expect(DOCS.length).toBeGreaterThan(0);
    const ids = DOCS.map((d) => d.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const d of DOCS) {
      expect(d.id).toBeTruthy();
      expect(d.group).toBeTruthy();
      expect(d.title).toBeTruthy();
      expect(d.summary).toBeTruthy();
      expect(d.body.length).toBeGreaterThan(0);
    }
    expect(getDoc("understanding-policy")).toBeDefined();
    expect(getDoc("nope")).toBeUndefined();
  });

  it("orders groups by GROUP_ORDER", () => {
    const groups = groupedDocs().map((g) => g.group);
    const known = groups.filter((g) => GROUP_ORDER.includes(g));
    expect(known).toEqual([...known].sort((a, b) => GROUP_ORDER.indexOf(a) - GROUP_ORDER.indexOf(b)));
  });

  it("exposes only id-bearing top-level headings as jump targets", () => {
    const policy = getDoc("understanding-policy");
    expect(policy).toBeDefined();
    const sections = docSections(policy!);
    expect(sections.map((s) => s.id)).toContain("in-a-policy");
    // Subsection headings (h3) carry ids for deep links but are not jump targets.
    expect(sections.map((s) => s.id)).not.toContain("protections");
    expect(sections.map((s) => s.id)).not.toContain("starting-point");
  });
});

describe("DocBody", () => {
  it("renders bold and code inline without injecting markup", () => {
    render(<DocBody blocks={[{ kind: "p", text: "must total **100** or `Save` is blocked" }]} />);
    expect(screen.getByText("100").tagName).toBe("STRONG");
    expect(screen.getByText("Save").tagName).toBe("CODE");
    // The asterisks and backticks are consumed, not shown.
    expect(screen.queryByText(/\*\*/)).toBeNull();
  });

  it("marks the highlighted table column", () => {
    const { container } = render(
      <DocBody blocks={[{ kind: "table", head: ["a", "b"], rows: [["x", "y"]], hi: 1 }]} />,
    );
    const hi = container.querySelectorAll("td.hi");
    expect(hi).toHaveLength(1);
    expect(hi[0]?.textContent).toBe("y");
  });

  it("draws a diagram: readable nodes, a toned branch, and a tone-classed connector", () => {
    const { container } = render(
      <DocBody
        blocks={[
          {
            kind: "diagram",
            title: "One file",
            legend: [{ tone: "keep", text: "the file is kept" }],
            steps: [
              { node: { text: "Start", shape: "terminal" } },
              {
                node: { text: "Spared?", shape: "decision" },
                enter: { label: "next", phase: "for each file" },
                branch: { label: "yes", node: { text: "Keep this file", tone: "keep" } },
              },
            ],
          },
        ]}
      />,
    );
    // Every node is real text, so the flow is readable, not a baked image.
    expect(screen.getByText("Spared?")).toBeInTheDocument();
    expect(screen.getByText("Keep this file")).toBeInTheDocument();
    // The keep branch and its connector both carry the verdict tone class.
    const branch = container.querySelector(".dd-branch.dd-keep");
    expect(branch).not.toBeNull();
    expect(branch?.querySelector(".dd-node.dd-keep")?.textContent).toContain("Keep this file");
    // The connector into step two shows its label and its phase divider.
    expect(screen.getByText("next")).toBeInTheDocument();
    expect(screen.getByText("for each file")).toBeInTheDocument();
  });
});

describe("DocsModal", () => {
  it("shows the asked-for doc, the index, and its section list", () => {
    render(
      <DocsModal docId="understanding-policy" anchor={undefined} nonce={1} onClose={() => {}} onNavigate={() => {}} />,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Understanding policy" })).toBeInTheDocument();
    // The index lists other docs too.
    expect(screen.getByRole("button", { name: /Tuning cheat sheet/ })).toBeInTheDocument();
    // The active doc's top-level sections appear as jump buttons.
    expect(screen.getByRole("button", { name: "What's in a policy" })).toBeInTheDocument();
  });

  it("falls back to the first doc for an unknown id", () => {
    render(<DocsModal docId="does-not-exist" anchor={undefined} nonce={1} onClose={() => {}} onNavigate={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: DOCS[0]!.title })).toBeInTheDocument();
  });
});

describe("useDocs / DocsProvider", () => {
  function Opener({ id, anchor }: { id: string; anchor?: string }) {
    const { openDoc } = useDocs();
    useEffect(() => {
      openDoc(id, anchor);
    }, [openDoc, id, anchor]);
    return null;
  }

  it("opens the requested doc from anywhere under the provider", async () => {
    // `await`, because the documents are their own chunk now: nothing of them is fetched until
    // the first Help press, so the modal arrives a tick after openDoc (P-4).
    render(
      <DocsProvider>
        <Opener id="arming" />
      </DocsProvider>,
    );
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Turning deletion on" })).toBeInTheDocument();
  });

  it("throws when used outside a provider", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    function Bare() {
      useDocs();
      return null;
    }
    expect(() => render(<Bare />)).toThrow(/DocsProvider/);
    spy.mockRestore();
  });
});

describe("the protections table", () => {
  // Rule 25: operator copy may only name a feature that is wired. This table hand-lists the
  // protections and their defaults, so it is a second copy of GATE_META that nothing kept
  // honest -- when the "unmanaged" gate was retired, its entry left GATE_META and the editor
  // while this row stayed, telling operators a protection shipped On that no policy carried.
  it("names only protections the policy editor can still show", () => {
    const protections = understandingPolicy.body.find(
      (b): b is Extract<typeof b, { kind: "table" }> => b.kind === "table" && b.head[0] === "Protection",
    );
    expect(protections).toBeDefined();

    const known = new Set(Object.values(GATE_META).map((m) => m.label));
    const listed = protections!.rows.map((r) => r[0] ?? "");

    expect(listed.length).toBeGreaterThan(0);
    expect(listed.filter((label) => !known.has(label))).toEqual([]);
  });
});
