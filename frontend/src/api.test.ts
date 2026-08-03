// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the client does with a reply it cannot use: a body that is not JSON, and a session the
// server no longer recognizes. Both used to leak past every surface's error handling -- the
// first as parser jargon, the second as a Dashboard full of "Not authenticated." with no way
// back to the login screen.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorizedHandler } from "./api";

/** One canned response, whatever is asked for. Built fresh per call: a body can only be
 *  read once, so a shared Response would make the second call look like an empty one. */
const reply = (body: string | null, init: ResponseInit = {}) =>
  vi.fn(() => Promise.resolve(new Response(body, { status: 200, ...init })));

beforeEach(() => setUnauthorizedHandler(() => {}));
afterEach(() => vi.unstubAllGlobals());

describe("a reply the client cannot parse", () => {
  it("is an ApiError, in plain language", async () => {
    // A forward-auth proxy whose sign-in expired answers 200 with an HTML login page: the
    // response is "ok", and JSON.parse throws a SyntaxError that is not an ApiError, so it
    // falls past every `instanceof ApiError` branch in the app.
    vi.stubGlobal("fetch", reply("<!doctype html><title>Sign in</title>"));

    await expect(api.safety()).rejects.toBeInstanceOf(ApiError);
    await expect(api.safety()).rejects.toThrow("Reaper got an unexpected reply from the server.");
  });

  it("is still fine when the body is simply empty", async () => {
    vi.stubGlobal("fetch", reply(null, { status: 204 }));
    await expect(api.safety()).resolves.toBeUndefined();
  });

  it("covers the queue, which reads its totals off the headers and parses by hand", async () => {
    vi.stubGlobal("fetch", reply("not json"));
    await expect(api.candidates("condemn", {}, 1, 0)).rejects.toBeInstanceOf(ApiError);
  });
});

describe("a session the server no longer recognizes", () => {
  it("tells the app once, so it can go back to the login screen", async () => {
    const lost = vi.fn();
    setUnauthorizedHandler(lost);
    vi.stubGlobal("fetch", reply('{"detail":"Not authenticated."}', { status: 401 }));

    await expect(api.safety()).rejects.toThrow("Not authenticated.");
    expect(lost).toHaveBeenCalledTimes(1);
  });

  it("stays quiet for the gate's own probe, which 401s for every signed-out visitor", async () => {
    const lost = vi.fn();
    setUnauthorizedHandler(lost);
    vi.stubGlobal("fetch", reply('{"detail":"Not authenticated."}', { status: 401 }));

    await expect(api.me()).rejects.toThrow("Not authenticated.");
    expect(lost).not.toHaveBeenCalled();
  });

  it("stays quiet when the password was wrong, which is a 403 and not a dead session", async () => {
    const lost = vi.fn();
    setUnauthorizedHandler(lost);
    vi.stubGlobal(
      "fetch",
      reply('{"detail":"That password didn\'t match. Deletion stays off."}', { status: 403 }),
    );

    await expect(api.setDeletion(true, "nope")).rejects.toThrow("Deletion stays off.");
    expect(lost).not.toHaveBeenCalled();
  });
});

describe("a failure with nothing of Reaper's in it", () => {
  it("says something a person can read, not the HTTP status", async () => {
    // A reverse proxy during a container restart answers with its own HTML and no
    // `detail`. Every component renders error.message verbatim, so the old fallback put a
    // bare "Request failed (502)." across the queue, the reap sheet and every settings
    // panel (U-14, rule 21).
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("fetch", reply("<html>502 Bad Gateway</html>", { status: 502 }));

    await expect(api.safety()).rejects.toThrow("Reaper couldn't reach the server. Try again.");
    // The status is not lost, it just stops being the operator's problem.
    expect(String(warn.mock.calls[0]?.[0])).toContain("HTTP 502");
    warn.mockRestore();
  });

  it("does not blame the server for a refusal that came from the request", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("fetch", reply("{}", { status: 409 }));

    await expect(api.safety()).rejects.toThrow("Reaper couldn't do that. Try again.");
    warn.mockRestore();
  });

  it("still prefers the server's own words whenever there are any", async () => {
    vi.stubGlobal("fetch", reply('{"detail":"Another reap is already running."}', { status: 409 }));
    await expect(api.safety()).rejects.toThrow("Another reap is already running.");
  });
});

describe("every call goes through the one wrapper", () => {
  // Four call sites -- the paged queue, the two binary downloads and the raw-body upload --
  // used to hand-roll their own fetch, so `request()` only looked like a choke point and a
  // cross-cutting change reached three quarters of the surface. They now share `fetchApi`,
  // and these are the assertions that say so: each of them is asked for a dead session and a
  // server failure, the two things every call has to report the same way (R-3).
  const paths: Array<[string, () => Promise<unknown>]> = [
    ["the paged queue", () => api.candidates("condemn", {}, 1, 0)],
    ["the log download", () => api.downloadLogs()],
    ["the backup download", () => api.downloadBackup()],
    ["the restore upload", () => api.restorePrepare(new File(["x"], "b.reaper"))],
  ];

  it.each(paths)("reports a dead session from %s", async (_name, call) => {
    const lost = vi.fn();
    setUnauthorizedHandler(lost);
    vi.stubGlobal("fetch", reply('{"detail":"Not authenticated."}', { status: 401 }));

    await expect(call()).rejects.toBeInstanceOf(ApiError);
    expect(lost).toHaveBeenCalledTimes(1);
  });

  it.each(paths)("speaks plainly about a server failure from %s", async (_name, call) => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal("fetch", reply("<html>502 Bad Gateway</html>", { status: 502 }));

    await expect(call()).rejects.toThrow("Reaper couldn't reach the server. Try again.");
    warn.mockRestore();
  });

  it("sends the CSRF header on all four", async () => {
    // The downloads succeed here, and jsdom has no navigation, so the anchor click that
    // saves the file is stubbed out rather than left to log "Not implemented" at the run.
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const seen: Array<Record<string, string>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_path: string, init: RequestInit) => {
        seen.push((init.headers ?? {}) as Record<string, string>);
        return Promise.resolve(new Response("{}", { status: 200 }));
      }),
    );
    for (const [, call] of paths) await call().catch(() => {});
    expect(seen).toHaveLength(4);
    for (const headers of seen) expect(headers["X-Reaper-CSRF"]).toBe("1");
    click.mockRestore();
  });
});

describe("starting a Plex sign-in", () => {
  // The window plex.tv opens is closed by a page plex.tv forwards it to, and the address of
  // that page can only come from the browser: every proxy in front of Reaper rewrites Host,
  // so a URL the server builds points somewhere the operator is not (#372).
  const sent = (fetchMock: ReturnType<typeof vi.fn>) =>
    JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body));

  it.each([
    ["sign-in", () => api.plexStart()],
    ["the Settings re-link", () => api.plexLinkStart()],
  ])("tells the server where this browser is, for %s", async (_name, call) => {
    const fetchMock = reply(JSON.stringify({ pin_id: 1, auth_url: "https://app.plex.tv/auth" }));
    vi.stubGlobal("fetch", fetchMock);

    await call();

    // window.location.origin under jsdom. Asserted against the live value rather than a
    // literal, because pinning "http://localhost:3000" would pass just as well if the code
    // sent a hardcoded string instead of asking the browser.
    expect(sent(fetchMock)).toEqual({ forward_origin: window.location.origin });
    expect(window.location.origin).toBeTruthy();
  });
});
