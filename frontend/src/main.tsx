// SPDX-License-Identifier: AGPL-3.0-or-later

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Reaper's data changes only when a scan runs, and a scan invalidates everything
      // explicitly. Refetching on every window focus would re-run the simulator on each
      // alt-tab for no new information.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
      retry: 1,
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("No #root element; index.html is not what we think it is.");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
