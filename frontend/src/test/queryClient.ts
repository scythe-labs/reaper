// SPDX-License-Identifier: AGPL-3.0-or-later
// The one QueryClient every test tree renders under.
//
// React Query's defaults are written for a browser someone is using: it retries a failed read,
// and it refetches when the window regains focus. Both are right in production and both inject
// async a test never asked for, so every suite here had already turned off `retry` by hand --
// 31 hand-rolled clients, each repeating the same object, and none of them turning off the
// second one.
//
// `refetchOnWindowFocus` is the one that bites, because user-event's click fires real focus
// events: a click on any focusable element refetches every stale query, the refetch resolves
// after the assertions, and React reports "an update was not wrapped in act(...)" naming a
// component the test never touched. That is not a race and not the test's own async -- it is a
// production behavior nothing in a test is meant to trigger. It cost one wrong diagnosis: the
// warning was read as an unfixable React Query scheduling artifact and excluded from the act
// gate, which left the warning printing on a green CI run with no way to act on it.
//
// Anything a test genuinely varies (a rejected read, a query held pending) is set on the query
// itself or its mock, never by loosening these two. Rule 136.
import { QueryClient } from "@tanstack/react-query";

export function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}
