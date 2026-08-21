// SPDX-License-Identifier: AGPL-3.0-or-later
// One class, three sites: a connection-test badge must describe the credentials it was actually
// computed from, never whatever is in the boxes now (rule 85). `setTest` was write-only at every
// one of them -- called on a result and cleared, at most, by a save or a fresh press -- so editing
// the thing that was tested left the badge standing (#178).
//
// The population is three, and `ServiceModal`'s is pinned in ServiceModal.test.tsx ("what the
// connection badge vouches for") because that form owns the Test button. This file drives the two
// in ServicesPanel.tsx and NotificationsPanel.tsx, which are the two an operator reaches from a
// row rather than from a form:
//   - the saved service card, whose address changes underneath it when the modal saves;
//   - the Discord row, whose URL box is editable right beside the badge.
// Fix one of these and leave a sibling and the class is half-closed with no test saying so, which
// is what rule 72 is for.
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import type { Instance } from "../api";
import { fill } from "../test/forms";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { NotificationsPanel } from "./NotificationsPanel";
import { ServicesPanel } from "./ServicesPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const PASSED = { ok: true, detail: "Reached", version: "4.0.1" };
// The Discord webhook test sends a typed reason rather than `PASSED`'s free-form `detail`
// (docs/history/I18N_PLAN.md §5); composed by NotificationsPanel into "Posted a test
// message to your Discord channel." (`services.discord.testResult.posted`).
const DISCORD_PASSED = { ok: true, reason: { k: "posted" }, version: "4.0.1" };

function sonarr(overrides: Partial<Instance> = {}): Instance {
  return {
    id: 3,
    kind: "sonarr",
    name: "Main",
    base_url: "http://10.0.0.5:8989",
    external_url: null,
    enabled: true,
    verify_tls: true,
    add_import_exclusion: false,
    plex_library_map: {},
    service_instance_map: {},
    has_key: true,
    detected_version: null,
    // Never tested server-side, so the card has no stored result to fall back to and the badge on
    // screen can only be the local one. A `last_ok_at` here would answer in its place and the
    // assertions could not tell the two apart (rule 141).
    last_ok_at: null,
    last_error: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.plexLibraries.mockResolvedValue([]);
  apiMock.testSavedInstance.mockResolvedValue(PASSED);
  apiMock.testWebhook.mockResolvedValue(DISCORD_PASSED);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
});

const badge = () => document.querySelector(".test-badge");

describe("the badge on a saved service card", () => {
  it("goes when the address it was computed for changes under it", async () => {
    // Editing a service through the modal invalidates ["instances"], so this card re-renders with a
    // new address while its local result is untouched. That is the trigger: no press, no typing in
    // this row at all, and the badge went on vouching for the host that passed.
    apiMock.instances.mockResolvedValue([sonarr()]);
    const client = testQueryClient();
    renderWithProviders(
      <>
        <Announcer />
        <ServicesPanel />
      </>,
      { client },
    );
    const user = userEvent.setup();

    const press = await screen.findByRole("button", { name: /Test/ });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);
    await waitFor(() => expect(badge()!.textContent).toContain("Reached"));

    // The save someone made in the modal, arriving the only way this card can see it.
    apiMock.instances.mockResolvedValue([sonarr({ base_url: "http://10.0.0.6:8989" })]);
    await client.invalidateQueries({ queryKey: ["instances"] });

    await waitFor(() => expect(screen.getByText("http://10.0.0.6:8989")).toBeInTheDocument());
    expect(badge()).toBeNull();
    expect(screen.getByText("Not tested yet")).toBeInTheDocument();
  });
});

describe("the badge on the Discord row", () => {
  const urlBox = () => screen.getByLabelText(/Webhook URL/i);

  async function sendATestMessage() {
    renderWithProviders(
      <>
        <Announcer />
        <NotificationsPanel />
      </>,
    );
    const user = userEvent.setup();
    await fill(user, urlBox(), "https://discord.com/api/webhooks/1/aaa");
    const press = await screen.findByRole("button", { name: "Send test message" });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);
    await waitFor(() => expect(badge()!.textContent).toContain("Posted"));
    return user;
  }

  it("goes when the URL it was computed for is edited", async () => {
    // Save, Remove and a fresh press each cleared this already. Editing the box did not, and the
    // box sits directly beside the badge, so this is the one an operator hits without leaving.
    const user = await sendATestMessage();

    await user.type(urlBox(), "b");

    expect(badge()).toBeNull();
  });

  it("stands again for the exact URL it was sent to", async () => {
    const user = await sendATestMessage();
    await user.type(urlBox(), "b");
    expect(badge()).toBeNull();

    await user.type(urlBox(), "{backspace}");

    expect(badge()!.textContent).toContain("Posted");
    // Re-shown, never re-sent: nobody wants a second message in the channel for a backspace.
    expect(apiMock.testWebhook).toHaveBeenCalledTimes(1);
  });
});

describe("where a keyboard operator lands when a service card removes itself", () => {
  // The Confirm remove button is inside the card it destroys, so the card unmounts a refetch later
  // and takes it along: focus falls to `<body>` and the next Tab restarts at the top of Settings
  // (#173). The successor cannot live in the card, which is why the section above owns the move.
  //
  // Driven through a SINGLETON kind on purpose. Tautulli hides its Add button while its one card
  // exists, so the target is not merely off-screen at press time -- it does not exist, and only
  // mounts because `canAdd` flips in the same refetch that removes the card. A non-singleton kind
  // has the Add button up all along and cannot tell a hook that waits from one that does not.
  function tautulli(): Instance {
    return { ...sonarr(), id: 9, kind: "tautulli", name: "Stats" };
  }

  it("lands on the Add button, which only arrives once the card is gone", async () => {
    apiMock.instances.mockResolvedValue([tautulli()]);
    apiMock.deleteInstance.mockResolvedValue({ ok: true });
    const client = testQueryClient();
    renderWithProviders(
      <>
        <Announcer />
        <ServicesPanel />
      </>,
      { client },
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Remove Stats" }));
    // While the card is up, a singleton kind offers no Add at all.
    expect(screen.queryByRole("button", { name: /Add a Tautulli/ })).toBeNull();

    apiMock.instances.mockResolvedValue([]);
    await user.click(screen.getByRole("button", { name: "Confirm remove Stats" }));

    const add = await screen.findByRole("button", { name: /Add a Tautulli/ });
    await waitFor(() => expect(add).toHaveFocus());
  });
});
