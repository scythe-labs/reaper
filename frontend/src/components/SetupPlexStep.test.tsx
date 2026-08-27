// SPDX-License-Identifier: AGPL-3.0-or-later
// The wizard's Plex step, and the two paths on it that change which server is linked.
//
// The wizard's copy of the invalidation set can drift from the one `plexServerQueries.ts`
// declares without the suite noticing, since `PlexPanel.test.tsx` only pins the panel's copy.
//
// Like the panel's, these pin the invalidation rather than a stale grid: `testQueryClient`
// leaves `staleTime` at 0 where the app sets 30s, so under the suite every re-enable refetches
// anyway, and the symptom cannot be reproduced without pinning a fixture as hard as the
// component.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SetupStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_PLEX_STATUS } from "../test/apiFixtures";
import { OF_THE_LINKED_SERVER } from "../plexServerQueries";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { SetupPlexStep } from "./SetupPlexStep";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// Answered file-wide, because every mount here renders the linked step and reads both. Left
// unanswered, they resolve to `undefined`, which React Query treats as a failed read, so every
// assertion below would run against the step's could-not-read branch instead. A describe that
// varies either one sets its own value after the reset, the way `plexResources` already does.
beforeEach(() => {
  apiMock.plexStatus.mockResolvedValue(DEFAULT_PLEX_STATUS);
  apiMock.plexLibraries.mockResolvedValue([]);
});

function setup(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    complete: false,
    plex_linked: true,
    has_instances: false,
    has_policy: false,
    admin_password_set: false,
    ...overrides,
  } as SetupStatus;
}

/** A mount whose invalidations the test can read back. The spy calls through, so the step still
 *  refetches and reaches its linked render. */
function renderRecordingInvalidations(status: SetupStatus): string[] {
  const client = testQueryClient();
  const invalidated: string[] = [];
  const passThrough = client.invalidateQueries.bind(client);
  vi.spyOn(client, "invalidateQueries").mockImplementation((filters) => {
    invalidated.push(JSON.stringify(filters?.queryKey));
    return passThrough(filters);
  });
  renderWithProviders(<SetupPlexStep setup={status} onNext={() => {}} />, { client });
  return invalidated;
}

function expectWholeSetDropped(invalidated: string[]) {
  // Read from the shared declaration here, unlike `PlexPanel.test.tsx`, which writes the list out
  // to catch a key silently dropped from it. One transcription of the set is the point of
  // hoisting it. A second transcription here would be the same duplication this guard exists to
  // avoid, and the panel's copy is the one positioned to notice a dropped key.
  for (const key of OF_THE_LINKED_SERVER) {
    expect(invalidated, `${JSON.stringify(key)} is not "of the linked server" any more`).toContain(
      JSON.stringify([...key]),
    );
  }
}

describe("changing which server is linked, from the wizard", () => {
  it("stops trusting every row about the old server when you switch servers", async () => {
    const user = userEvent.setup();
    apiMock.plexResources.mockResolvedValue({
      source: "plex.tv",
      servers: [
        { name: "First", machine_identifier: "machine-1", current: true, connections: [] },
        { name: "Second", machine_identifier: "machine-2", current: false, connections: [] },
      ],
    });
    apiMock.plexSwitchServer.mockResolvedValue(undefined);
    const invalidated = renderRecordingInvalidations(setup());

    // A `<select>`, whose accessible name holds still while the resources query loads, so it
    // gates nothing on its own, and this waits for the control instead of the page. user-event
    // reports a disabled target as success, so acting one turn early would do nothing at all.
    const picker = await screen.findByLabelText("Server");
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, "machine-2");

    await waitFor(() => expect(apiMock.plexSwitchServer).toHaveBeenCalledWith("machine-2"));
    await waitFor(() => expectWholeSetDropped(invalidated));
    // Switching servers does not change whether the install is configured, so this one stays put.
    expect(invalidated).not.toContain(JSON.stringify(["setup"]));
  });

  it("has no accessibility violations in its linked state", async () => {
    // Two servers, because the Server picker only renders when there is a choice to make. With
    // one server, it is absent, and the audit would read a tree missing the control it is here
    // for.
    apiMock.plexResources.mockResolvedValue({
      source: "plex.tv",
      servers: [
        { name: "First", machine_identifier: "machine-1", current: true, connections: [] },
        { name: "Second", machine_identifier: "machine-2", current: false, connections: [] },
      ],
    });
    const { container } = renderWithProviders(<SetupPlexStep setup={setup()} onNext={() => {}} />, {
      client: testQueryClient(),
    });

    // Wait for the pickers, so the audit reads the tree the operator gets rather than the
    // loading one. Not `pageLevel`: this is a step inside `SetupWizard`'s card, not a page.
    await screen.findByLabelText("Server");
    await expectNoA11yViolations(container);
  });

  it("stops trusting them when a link lands here, too", async () => {
    apiMock.plexStatus.mockResolvedValue({
      linked: false,
      name: null,
      connection_uri: null,
      last_ok_at: null,
      verify_tls: true,
      web_url: null,
    });
    apiMock.plexResources.mockResolvedValue({ source: "plex.tv", servers: [] });
    apiMock.plexLinkStart.mockResolvedValue({ pin_id: 1, auth_url: "https://plex.tv/link" });
    apiMock.plexLinkPoll.mockResolvedValue({ status: "ok", server: null, servers: null });
    vi.spyOn(window, "open").mockReturnValue(null);
    const invalidated = renderRecordingInvalidations(setup({ plex_linked: false }));

    const start = await screen.findByRole("button", { name: "Sign in with Plex" });
    // `fireEvent`, not user-event: the poll runs on a two-second interval, so this needs fake
    // timers, and user-event schedules its own on the real clock. The advances are awaited inside
    // `act`, or the poll would settle after the assertions run and fail as a state update outside
    // `act`.
    vi.useFakeTimers();
    try {
      fireEvent.click(start);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0); // the PIN request settles
        await vi.advanceTimersByTimeAsync(2100); // the first poll, which comes back linked
      });
    } finally {
      vi.useRealTimers();
    }

    await waitFor(() => expectWholeSetDropped(invalidated));
    // Linking DOES change it: the install just became more configured than it was.
    expect(invalidated).toContain(JSON.stringify(["setup"]));
  });
});
