// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Plex library list, and the one rule for filling it when it has never been filled.
//
// `GET /plex/libraries` answers "as last synced", so on an install that has never synced it
// answers `[]`, indistinguishable at the call site from a Plex server with no video
// libraries. Three screens read this list: the Plex settings panel, the wizard's Plex step,
// and the service editor's library pickers.
//
// The server syncs on link and on server-switch, which is where the list first becomes
// knowable and covers every new install. This hook covers installs that linked Plex before
// that existed: whichever of the three screens they open first fills the list, rather than
// each screen having to remember. It syncs once per mount, and only for a list that
// genuinely came back empty, never for one that failed to load, which is a different state
// and must not be papered over with a write.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "./api";

export function usePlexLibraries({ enabled = true }: { enabled?: boolean } = {}) {
  const queryClient = useQueryClient();
  const libraries = useQuery({
    // The one spelling of this key. Caching it under a second one would let a list
    // refreshed after a sync keep offering libraries that had just been removed, until the
    // page reloaded.
    queryKey: ["plex-libraries"],
    queryFn: api.plexLibraries,
    enabled,
  });
  const sync = useMutation({
    mutationFn: api.syncPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });

  // The ref makes this once-per-mount even though the mutation object's identity changes on
  // every render. `libraries.data` being an empty array is the trigger, not falsy: a failed
  // read leaves `data` undefined, and re-syncing on that would answer a read failure with a
  // write.
  const autoSynced = useRef(false);
  useEffect(() => {
    if (enabled && libraries.data && libraries.data.length === 0 && !autoSynced.current) {
      autoSynced.current = true;
      sync.mutate();
    }
  }, [enabled, libraries.data, sync]);

  return { libraries, sync };
}
