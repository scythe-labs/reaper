// SPDX-License-Identifier: AGPL-3.0-or-later
// The one way a test mounts a tree that reads the query cache.
//
// Every component under test sits below `<QueryClientProvider>` in the app, so every test that
// mounts one had to build that tree by hand: a `testQueryClient()`, a provider, the component,
// and a closing tag. It was written 87 times across 35 files, inside 55 file-local `render*`
// helpers that differ only in which component they mount and which props they pass.
//
// The duplication is not the whole cost. Two things a test needs are easy to get subtly wrong
// when the provider is hand-written, and both are handled here:
//
//   * **`rerender` keeps its providers.** `render(<Provider>{ui}</Provider>)` returns a
//     `rerender` that replaces the WHOLE tree, so `rerender(<X prop={next} />)` drops the
//     provider and every hook below it starts reading from no cache. Passing the provider as
//     testing-library's `wrapper` instead makes `rerender` re-wrap, so it takes the component
//     alone and the cache survives the re-render, which is what a test comparing two prop
//     values means.
//   * **The client comes back.** A test that seeds the cache or spies on `invalidateQueries`
//     needs the client the tree is actually reading, and hand-rolled trees got it by declaring
//     a local `const queryClient` above the render. It is returned here instead.
//
// Rule 136 is why the client is `testQueryClient()` rather than `new QueryClient` -- read its
// note in `queryClient.ts` before reaching for a different one.
//
// Seeding stays at the call site, deliberately: `{ client: seedSettings(testQueryClient()) }`
// rather than a `seed: true` option. `seedSettings`' own contract is "never seed a key the test
// varies," and a boolean flag is exactly the shape that gets copied into the suite that rejects
// `api.profile` to see the failed-read branch, where it would quietly answer the read the test
// is trying to fail.
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import {
  render,
  renderHook,
  type RenderHookOptions,
  type RenderHookResult,
  type RenderOptions,
  type RenderResult,
} from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { DocsProvider } from "../docs/DocsContext";
import { testQueryClient } from "./queryClient";

/** Pass `client` when the test built one it needs a handle on before the first paint -- one it
 *  seeded, or one whose `invalidateQueries` it spied on. Otherwise a fresh one is made and
 *  handed back. */
type ProviderOptions = { client?: QueryClient };

function providerWrapper(client: QueryClient) {
  return function Providers({ children }: { children: ReactNode }) {
    // `DocsProvider` for the same reason as the query client: `App` wraps everything in it, so a
    // component holding a help link renders in the app and throws in a test. It fetches nothing
    // and mounts nothing while closed, so it costs the tests that hold no link nothing at all.
    return (
      <QueryClientProvider client={client}>
        <DocsProvider>{children}</DocsProvider>
      </QueryClientProvider>
    );
  };
}

/** Mount `ui` under the query provider. Returns testing-library's usual result plus the client
 *  the tree is reading, so a test can seed it, read it back, or assert on what was invalidated. */
export function renderWithProviders(
  ui: ReactElement,
  { client, ...options }: ProviderOptions & Omit<RenderOptions, "wrapper"> = {},
): RenderResult & { client: QueryClient } {
  const queryClient = client ?? testQueryClient();
  return {
    ...render(ui, { wrapper: providerWrapper(queryClient), ...options }),
    client: queryClient,
  };
}

/** The same tree for a hook test, which otherwise hand-builds a `wrapper` with `createElement`
 *  because the file is `.ts` and cannot spell JSX. */
export function renderHookWithProviders<Result, Props>(
  hook: (initialProps: Props) => Result,
  { client, ...options }: ProviderOptions & Omit<RenderHookOptions<Props>, "wrapper"> = {},
): RenderHookResult<Result, Props> & { client: QueryClient } {
  const queryClient = client ?? testQueryClient();
  return {
    ...renderHook(hook, { wrapper: providerWrapper(queryClient), ...options }),
    client: queryClient,
  };
}
