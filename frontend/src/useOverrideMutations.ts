// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Spare and Reap, wherever they are offered. The cards, an expanded show's season rows and
// the why-panel all set the same override, and all of them must refresh the same caches --
// so the list of caches an override touches is written once, here, and every surface that
// gains one only has to be added in a single place.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Override } from "./api";

export function useOverrideMutations() {
  const queryClient = useQueryClient();

  // An override changes what the queue lists, what an expanded show's all-seasons list,
  // the show panel and an open why-panel show, and the Reap page's breakdown (a spare drops
  // an item out of the net, a hand reap adds one), so every cache refreshes together.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["candidates"] });
    void queryClient.invalidateQueries({ queryKey: ["group"] });
    void queryClient.invalidateQueries({ queryKey: ["candidate"] });
    void queryClient.invalidateQueries({ queryKey: ["reap-breakdown"] });
  };

  const setOverride = useMutation({
    mutationFn: ({ key, decision }: { key: string; decision: Override }) =>
      api.override(key, decision),
    onSuccess: refresh,
  });
  const clearOverride = useMutation({
    mutationFn: (key: string) => api.clearOverride(key),
    onSuccess: refresh,
  });

  return { setOverride, clearOverride, refresh };
}
