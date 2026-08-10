// SPDX-License-Identifier: AGPL-3.0-or-later
// A cross-page jump aims one view, and the aim dies when that view goes off screen.
//
// `App` holds where the app is pointed as one `Focus`, acted on once per nonce by whichever
// view it names. The load-bearing half is the DROP: a Back press restores `view` through the
// raw setter and runs no handler, and the view unmounts on the way out, taking the ref that
// counts the nonce with it. So an aim left behind is replayed in full the next time that view
// mounts -- which is how an operator who backed out of a Scales jump landed on a one-title
// Review list wearing a search chip they never typed.
//
// The Review arm of that is pinned in `App.test.tsx`, end to end through the real queue. This
// file is the other two, which had nothing: `App` used to hold three parallel focuses and drop
// them by name, and the line doing it had already been wrong once, dropping only the one that
// had been shown to replay. Each route is driven the same way:
//
//   jump in, leave by the section nav, press Back, land on the page unaimed
//
// **Leave by the NAV, and come back by BACK.** Both halves are load-bearing and each was got
// wrong once here. Leaving by another JUMP proves nothing: a jump writes its own aim over the
// old one, so the page reads unaimed whether or not anything drops it. And returning by the nav
// proves nothing either, because the nav is the one route out that runs a handler. Only Back
// restores `view` through the raw setter, which is the shape a leftover aim replays through.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthUser, Safety, Snapshot } from "./api";
import { DEFAULT_GENERAL, DEFAULT_PROFILE, DEFAULT_UPDATE, IDLE_SCAN } from "./test/apiFixtures";
import { renderWithProviders } from "./test/renderWithProviders";
import { App } from "./App";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("./test/apiMock")).makeApiMock(),
}));

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  api: apiMock,
}));

// The two routes stand in for themselves, and each prints the aim it was handed. What the real
// pages do with that aim is their own tests' subject; this file is about whether the shell is
// still holding one. Stubbing them also keeps the two heaviest trees in the app, and every read
// they make, out of a test that mounts neither for its content.
vi.mock("./components/PolicyEditor", () => ({
  PolicyEditor: ({ focus }: { focus?: { section: string } | null }) => (
    <p>policy aimed at: {focus?.section ?? "nothing"}</p>
  ),
}));
vi.mock("./components/Settings", () => ({
  Settings: ({ initialPanel }: { initialPanel?: string }) => (
    <p>settings aimed at: {initialPanel ?? "nothing"}</p>
  ),
}));

const USER: AuthUser = {
  id: 1,
  username: "owner",
  provider: "local",
  thumb_url: null,
  via_recovery: false,
};

/** Deletion off, which is the branch whose banner carries the link into Policy → Deletion. */
const SAFETY: Safety = {
  destructive_enabled: false,
  has_password: true,
  recovery_mode: false,
  note: null,
};

const SNAPSHOT: Snapshot = {
  id: 1,
  created_at: "2026-01-01T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 12,
  degraded: false,
  degraded_reason: null,
  condemned: 3,
  protected: 4,
  abstained: 5,
  unknown_size_items: 0,
  reclaimable_bytes: 0,
};

beforeEach(() => {
  apiMock.me.mockResolvedValue(USER);
  apiMock.safety.mockResolvedValue(SAFETY);
  apiMock.setupStatus.mockResolvedValue({ complete: true, steps: [] });
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.latestSnapshot.mockResolvedValue(SNAPSHOT);
  apiMock.candidates.mockResolvedValue({
    items: [],
    groups: [],
    total: 0,
    total_bytes: 0,
    unknown_size: 0,
    offset: 0,
    snapshot_id: 1,
  });
  apiMock.general.mockResolvedValue(DEFAULT_GENERAL);
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  // An update is on offer, because the user menu's "Update available" item is the app's one
  // always-mounted jump into Settings. Every other route there (the stale-scan notice, the reap
  // plan's Plex link) is rendered by a view this walk has already left.
  apiMock.update.mockResolvedValue({ ...DEFAULT_UPDATE, update_available: true, latest: "0.2.0" });
  apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: true, will_reap: 0, condemned_by: [] });
  // Nothing running, and the queue's two filter suggesters have nothing to suggest. These three
  // are rule 135's documented blind spot rather than its gate: all three reads go through an
  // arrow or return a bare value, so an unanswered one renders the tree's empty branch and says
  // so only on the console. Answered here, or this file ships noise it exists to notice.
  apiMock.reapStatus.mockResolvedValue({ running: false });
  apiMock.vocabularyValues.mockImplementation((field: string) =>
    Promise.resolve({ field, values: [] }),
  );
});

// jsdom carries one session history across a whole file, and a test that ends with back-nav
// layers still open has its entries handed back one deferred task at a time. Those are real
// popstates: arriving after the next test's provider is up, they read as Back presses and eat
// the ones it means to send. Drain them while nothing is listening, then clear the marker the
// provider's mount-time reconcile keys on, the same reset `App.test.tsx` and `backnav.test.tsx`
// do between their tests. There is one test below and it ends with two layers unpopped, so this
// is here for the second one (rule 72, and the file's own shape invites it).
beforeEach(async () => {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  history.replaceState(null, "");
});

/** A Back press, the way `backnav.test.tsx` drives one: the provider parks a sentinel entry per
 *  layer and listens for the popstate that handing one back produces. */
async function back() {
  await act(async () => {
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

/** Let jsdom's deferred history work land before the next press. jsdom carries one session
 *  history across a whole file and hands entries back a task at a time, and those arrive as
 *  real popstates: press Back before they settle and the press is what gets swallowed. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("a jump's aim", () => {
  it("is gone when the view it aimed at is reached again by Back", async () => {
    const person = userEvent.setup();
    renderWithProviders(<App />);

    // Into Policy, aimed at Deletion, from the banner that says deletion is off.
    await person.click(await screen.findByRole("button", { name: /turn deletion on in policy/i }));
    expect(await screen.findByText("policy aimed at: deletion")).toBeInTheDocument();

    // Out again by the section nav, which records the Back step that comes back here.
    await settle();
    await person.click(screen.getByRole("button", { name: "Review" }));
    await screen.findByRole("searchbox", { name: /search titles/i });

    // Back to Policy. The Deletion section is not re-opened: the operator asked for the page,
    // not for the jump that first took them there.
    await settle();
    await back();
    expect(await screen.findByText("policy aimed at: nothing")).toBeInTheDocument();

    // The Settings route, driven the same way rather than argued from the shape of the one
    // above (rule 145). It reaches Settings through the user menu's update item, since the
    // section nav is the affordance this walk needs to leave BY.
    await settle();
    await person.click(screen.getByRole("button", { name: /owner, update available/i }));
    await person.click(await screen.findByRole("button", { name: /^update available$/i }));
    expect(await screen.findByText("settings aimed at: about")).toBeInTheDocument();

    // Clicking the tab you are ALREADY on is not a visit and must leave the aim alone (B-23).
    // Settings is mounted under the aim's nonce, so dropping it here remounts the subtree and
    // throws away whatever is typed into it. This used to be an `if (next !== view)` guard in the
    // nav handler and is now the effect's `[view]` dep, which is a condition nothing states in
    // words, so it is asserted rather than read: an unconditional drop on every tab click prints
    // "nothing" here and is green everywhere else in the suite.
    await settle();
    await person.click(screen.getByRole("button", { name: "Settings" }));
    expect(screen.getByText("settings aimed at: about")).toBeInTheDocument();

    await settle();
    await person.click(screen.getByRole("button", { name: "Review" }));
    await screen.findByRole("searchbox", { name: /search titles/i });

    await settle();
    await back();
    // Settings opens on its own default panel, not on About. It is keyed on the aim's nonce, so
    // a leftover aim would also remount the whole subtree and throw away anything typed into it.
    await waitFor(() => expect(screen.getByText("settings aimed at: nothing")).toBeInTheDocument());
  });
});
