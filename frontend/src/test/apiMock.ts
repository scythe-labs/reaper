// SPDX-License-Identifier: AGPL-3.0-or-later
// One mock that answers every api function, for the tests that replace `../api` wholesale.
//
// Thirty-five test files hand-declared the subset of the api their tree reads, ranging from one
// function to thirty-two. That list is not the test's own business: it is a property of the
// component tree, it changes whenever a nested hook starts reading something, and the file that
// has to change is one nobody editing the hook is looking at. Rule 135 is the standing answer --
// a mock gap fails the run rather than warning -- and this closes the same gap one step earlier,
// by leaving no subset to get wrong.
//
// **Why this is safe to widen, given rule 135's own risk note.** Supplying a function a file used
// to omit could in principle answer a read the test meant to fail. It cannot, in either shape a
// tree performs the read:
//
//   * `queryFn: api.foo` -- an omitted `foo` is `undefined`, which `src/test/setup.ts` already
//     FAILS the run on. So no passing test relies on that absence today; there are none to break.
//   * `queryFn: () => api.foo(x)` -- an omitted `foo` throws a TypeError inside the arrow, which
//     React Query files as an ordinary rejection and the tree renders its failed-read branch. An
//     unconfigured `vi.fn()` returns `undefined`, which React Query also rejects. Same branch,
//     same silence. This is rule 135's documented blind spot, and it is unchanged either way.
//
// A test that means to reject a read still says so with `mockRejectedValue`, which is what the
// suite already does; absence was never the way to ask for it.
//
// **The mutations are the half nothing was watching.** `setup.ts` fails a query with no
// `queryFn`; it says nothing about a mutation with no `mutationFn`, because React Query does not
// announce that one. `removeApiKey` was missing from the General panel's mock until the Remove
// path needed driving, and it went missing SILENTLY: a test pressing Remove would have failed on
// an undefined call rather than on the thing it was checking. So the completeness here is not a
// tidier spelling of a gate that already existed -- for every mutation in the module, it is the
// only thing standing between a gap and a confusing failure.
//
// The key list is checked against the real module by `apiMock.test.ts`, in both directions, so a
// function added to `api.ts` fails here rather than going missing from thirty-five trees at once
// (rule 103).
import { vi } from "vitest";

import type { api } from "../api";

/** Every api function as a `vi.fn()`, typed against the real one so a call site that mocks the
 *  wrong shape is a compile error rather than a passing test. */
export type ApiMock = { [K in keyof typeof api]: ReturnType<typeof vi.fn> };

/** A fresh set per file. Never share one across files: `vi.fn()` carries call history, and
 *  vitest gives each test file its own module registry, so a shared instance would leak counts
 *  between them (rule 133). */
export function makeApiMock(): ApiMock {
  return {
    latestSnapshot: vi.fn(),
    candidates: vi.fn(),
    candidate: vi.fn(),
    group: vi.fn(),
    setupStatus: vi.fn(),
    instances: vi.fn(),
    createInstance: vi.fn(),
    updateInstance: vi.fn(),
    deleteInstance: vi.fn(),
    instanceRootFolders: vi.fn(),
    instanceSeerrServices: vi.fn(),
    testInstance: vi.fn(),
    testSavedInstance: vi.fn(),
    plexStatus: vi.fn(),
    setPlexSettings: vi.fn(),
    plexLinkStart: vi.fn(),
    plexLinkPoll: vi.fn(),
    plexUnlink: vi.fn(),
    plexResources: vi.fn(),
    plexSwitchServer: vi.fn(),
    plexSetConnection: vi.fn(),
    plexLibraries: vi.fn(),
    syncPlexLibraries: vi.fn(),
    setPlexLibraries: vi.fn(),
    watchEvidence: vi.fn(),
    resetWatchEvidence: vi.fn(),
    forgetWatchEvidenceFor: vi.fn(),
    leavingSoonSettings: vi.fn(),
    setLeavingSoonSettings: vi.fn(),
    about: vi.fn(),
    update: vi.fn(),
    general: vi.fn(),
    saveGeneral: vi.fn(),
    revealApiKey: vi.fn(),
    generateApiKey: vi.fn(),
    removeApiKey: vi.fn(),
    logs: vi.fn(),
    setLogLevel: vi.fn(),
    downloadLogs: vi.fn(),
    backupInfo: vi.fn(),
    downloadBackup: vi.fn(),
    restorePrepare: vi.fn(),
    restoreConfirm: vi.fn(),
    restoreCancel: vi.fn(),
    restoreRestart: vi.fn(),
    schedule: vi.fn(),
    saveJobSchedule: vi.fn(),
    runJob: vi.fn(),
    safety: vi.fn(),
    setDeletion: vi.fn(),
    setAdminPassword: vi.fn(),
    notifications: vi.fn(),
    setWebhook: vi.fn(),
    clearWebhook: vi.fn(),
    testWebhook: vi.fn(),
    policy: vi.fn(),
    vocabulary: vi.fn(),
    vocabularyValues: vi.fn(),
    savePolicy: vi.fn(),
    validatePolicy: vi.fn(),
    simulate: vi.fn(),
    probePolicy: vi.fn(),
    seasonShape: vi.fn(),
    startScan: vi.fn(),
    scanStatus: vi.fn(),
    runs: vi.fn(),
    run: vi.fn(),
    createRun: vi.fn(),
    dryRun: vi.fn(),
    executeRun: vi.fn(),
    reapStatus: vi.fn(),
    stopRun: vi.fn(),
    profile: vi.fn(),
    saveProfile: vi.fn(),
    fairness: vi.fn(),
    person: vi.fn(),
    reapBreakdown: vi.fn(),
    plexTrash: vi.fn(),
    lists: vi.fn(),
    listConfigs: vi.fn(),
    addList: vi.fn(),
    editList: vi.fn(),
    removeList: vi.fn(),
    syncLists: vi.fn(),
    syncLeavingSoon: vi.fn(),
    override: vi.fn(),
    clearOverride: vi.fn(),
    me: vi.fn(),
    authContext: vi.fn(),
    plexStart: vi.fn(),
    plexPoll: vi.fn(),
    localLogin: vi.fn(),
    recover: vi.fn(),
    logout: vi.fn(),
  };
}
