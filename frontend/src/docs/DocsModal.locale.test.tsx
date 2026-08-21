// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The modal with a translated manual shipped for the locale: it suspends on the module once,
// then renders that manual entire, index and pane, tagged with the manual's language while the
// chrome around it keeps the UI's. Its own file because the loader is mocked at the module
// boundary, which is the one seam between the modal and the glob: the English path and the
// fallback are in docs.test.tsx against the real loader.

import { act, render, screen } from "@testing-library/react";
import { Suspense } from "react";
import { describe, expect, it, vi } from "vitest";

import { expectNoA11yViolations } from "../test/a11y";
import type { Doc } from "./blocks";
import { DocsModal } from "./DocsModal";
import { DOCS } from "./registry";

const XX: Doc[] = [
  {
    id: "understanding-policy",
    group: "Policy",
    title: "Politik verstehen",
    summary: "Was eine Policy ist.",
    body: [
      { kind: "h", text: "In einer Policy", id: "in-a-policy" },
      { kind: "p", text: "Ein Absatz." },
    ],
  },
  {
    id: "arming",
    group: "Safety",
    title: "Löschen einschalten",
    summary: "Der letzte Schritt.",
    body: [{ kind: "p", text: "Noch ein Absatz." }],
  },
];

// One promise, held across renders the way the real loader's cache holds it: `use()` settles
// on the thenable it was handed, and a fresh one per render would suspend forever.
const manual = Promise.resolve({ lng: "de", docs: XX });
vi.mock(import("./localized"), async (importOriginal) => ({
  ...(await importOriginal()),
  manualFor: () => manual,
}));

describe("DocsModal with a translated manual", () => {
  it("renders that manual entire, tagged with its language", async () => {
    // Inside an async act: the suspend resumes on a ping that arrives after render returns,
    // and in an act environment a ping outside any act scope is never flushed (measured: a
    // bare render() followed by findBy* waits out its full budget on the fallback).
    let container: HTMLElement | undefined;
    await act(async () => {
      container = render(
        <Suspense fallback={null}>
          <DocsModal
            docId="understanding-policy"
            anchor={undefined}
            nonce={1}
            onClose={() => {}}
            onNavigate={() => {}}
          />
        </Suspense>,
      ).container;
    });
    const pane = screen.getByRole("region", { name: "Politik verstehen" });
    expect(pane).toHaveAttribute("lang", "de");
    expect(screen.getByRole("heading", { level: 3, name: "Politik verstehen" })).toBeVisible();
    expect(screen.getByText("Ein Absatz.")).toBeVisible();

    // The index is the translated manual's, not English's, and its entries carry its tag.
    const other = screen.getByRole("button", { name: /Löschen einschalten/ });
    expect(other).toHaveAttribute("lang", "de");
    expect(screen.getByRole("button", { name: /In einer Policy/ })).toBeVisible();
    for (const doc of DOCS) expect(screen.queryByText(doc.title)).toBeNull();

    // The chrome keeps the UI's locale: the group heading and the kicker come from the catalog.
    expect(screen.getAllByText("Policy")).toHaveLength(2);
    expect(screen.getByRole("dialog", { name: "Help & docs" })).toBeVisible();

    await expectNoA11yViolations(container as HTMLElement);
  });
});
