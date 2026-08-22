// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Notifications' language select (docs/history/I18N_PLAN.md, phase 10b): which
// language the Discord Leaving Soon embed is written in. The choices come from the server's own
// shipped list (rule 66, `reaper.i18n.shipped_tags()`) rather than a copy this file could drift
// from, and the select writes immediately on change, the same shape GeneralPanel's expand-seasons
// and reverse-proxy selects already use.

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Announcer } from "../announce";
import type { Notifications } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { NotificationsPanel } from "./NotificationsPanel";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

// Two shipped tags, not one: a fixture pinning only "en" cannot prove a picked value reaches the
// request rather than the default surviving unread (rule 141).
function notifications(overrides: Partial<Notifications> = {}): Notifications {
  return { has_webhook: false, language: "en", languages: ["en", "de"], ...overrides };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.notifications.mockResolvedValue(notifications());
});

describe("the notification language select", () => {
  it("renders the stored language and every shipped choice, from the server's own list", async () => {
    renderWithProviders(<NotificationsPanel />);

    const select = await screen.findByLabelText("Language of Discord posts");
    await waitFor(() => expect(select).toBeEnabled());
    expect((select as HTMLSelectElement).value).toBe("en");

    // Names come from the browser's own language list (rule 66's "no hand-kept map"), not a
    // literal string this test would have to keep in step with a translation shipping.
    const names = new Intl.DisplayNames(["en"], { type: "language" });
    expect(screen.getByRole("option", { name: names.of("en")! })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: names.of("de")! })).toBeInTheDocument();

    await expectNoA11yViolations();
  });

  it("saves the picked language and shows the stored value once the write lands", async () => {
    renderWithProviders(
      <>
        <Announcer />
        <NotificationsPanel />
      </>,
    );

    const select = await screen.findByLabelText("Language of Discord posts");
    await waitFor(() => expect(select).toBeEnabled());

    // The response this save would really get, and what a refetch reads back afterward --
    // both stand in for the one round trip the mutation and its invalidate drive.
    apiMock.setNotificationLanguage.mockResolvedValue(notifications({ language: "de" }));
    apiMock.notifications.mockResolvedValue(notifications({ language: "de" }));

    const user = userEvent.setup();
    await user.selectOptions(select, "de");

    expect(apiMock.setNotificationLanguage).toHaveBeenCalledWith("de");
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe("de"));
    await screen.findByText("Discord notification language saved.");
  });
});
