// SPDX-License-Identifier: AGPL-3.0-or-later

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { setUnauthorizedHandler } from "./api";
import "./i18n";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Reaper's data changes only when a scan runs, and a scan invalidates everything
      // explicitly. Refetching on every window focus would re-run the simulator on each
      // alt-tab for no new information. (The safety state is the one exception, and asks
      // for both back: see useSafety.)
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: 1,
    },
  },
});

// A session the server no longer recognizes drops the app straight back to the login screen,
// which is what `["me"]` holding no user means to the gate in App. Written, not invalidated:
// a refetch would 401 too, and answering a 401 with a request that 401s is a loop.
setUnauthorizedHandler(() => queryClient.setQueryData(["me"], null));

const root = document.getElementById("root");
if (!root) throw new Error("No #root element; index.html is not what we think it is.");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
