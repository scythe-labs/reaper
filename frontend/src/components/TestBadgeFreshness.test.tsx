// SPDX-License-Identifier: AGPL-3.0-or-later
// One class, three sites: a connection-test badge must describe the credentials it was actually
// computed from, never whatever is in the boxes now (rule 85). `setTest` was write-only at every
// one of them -- called on a result and cleared, at most, by a save or a fresh press -- so editing
// the thing that was tested left the badge standing (#178).
//
// The population is three, and `ServiceModal`'s is pinned in ServiceModal.test.tsx ("what the
// connection badge vouches for") because that form owns the Test button. This file drives the two
// in Settings.tsx, which are the two an operator reaches from a row rather than from a form:
//   - the saved service card, whose address changes underneath it when the modal saves;
//   - the Discord row, whose URL box is editable right beside the badge.
// Fix one of these and leave a sibling and the class is half-closed with no test saying so, which
// is what rule 72 is for.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import type { Instance } from "../api";
import { testQueryClient } from "../test/queryClient";
import { NotificationsPanel, ServicesPanel } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    // Rule 135: everything the two trees read, not only what a test here names.
    instances: vi.fn(),
    testSavedInstance: vi.fn(),
    deleteInstance: vi.fn(),
    plexLibraries: vi.fn(),
    notifications: vi.fn(),
    setWebhook: vi.fn(),
    testWebhook: vi.fn(),
    clearWebhook: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const PASSED = { ok: true, detail: "Reached", version: "4.0.1" };

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
    api_path_prefix: "/api/v3",
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
  apiMock.testWebhook.mockResolvedValue(PASSED);
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
    render(
      <QueryClientProvider client={client}>
        <Announcer />
        <ServicesPanel />
      </QueryClientProvider>,
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
    render(
      <QueryClientProvider client={testQueryClient()}>
        <Announcer />
        <NotificationsPanel />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.type(urlBox(), "https://discord.com/api/webhooks/1/aaa");
    const press = await screen.findByRole("button", { name: "Send test message" });
    await waitFor(() => expect(press).toBeEnabled());
    await user.click(press);
    await waitFor(() => expect(badge()!.textContent).toContain("Reached"));
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

    expect(badge()!.textContent).toContain("Reached");
    // Re-shown, never re-sent: nobody wants a second message in the channel for a backspace.
    expect(apiMock.testWebhook).toHaveBeenCalledTimes(1);
  });
});
