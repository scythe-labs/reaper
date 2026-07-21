// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The docs system: the registry stays coherent, the block renderer is faithful, and the
// open-from-anywhere contract opens the right page (and deep-links to a section).

import { render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";
import { docSections } from "./blocks";
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

  it("opens the requested doc from anywhere under the provider", () => {
    render(
      <DocsProvider>
        <Opener id="arming" />
      </DocsProvider>,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
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
