// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Plex library list, and the one rule for filling it when it has never been filled.
//
// `GET /plex/libraries` answers "as last synced", so on an install that has never synced it
// answers `[]` -- which is indistinguishable, at the call site, from a Plex server with no
// video libraries. Three screens read this list (the Plex settings panel, the wizard's Plex
// step, and the service editor's library pickers) and only one of them knew to sync it, so the
// other two rendered an empty grid on a fresh install and told the operator to go press a
// button on a third screen (#384).
//
// The server now syncs on link and on server-switch, which is where the list first becomes
// knowable and covers every new install. This hook is the second half, for the installs that
// linked Plex *before* that shipped: whichever of the three screens they open first fills the
// list, rather than each screen having to remember. Once per mount, and only for a list that
// genuinely came back empty -- never for one that failed to load, which is a different state
// and must not be papered over with a write.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "./api";

export function usePlexLibraries({ enabled = true }: { enabled?: boolean } = {}) {
  const queryClient = useQueryClient();
  const libraries = useQuery({
    // The one spelling of this key. Cached under a second one, a list refreshed after a sync
    // went on offering libraries that had just been removed until the page was reloaded.
    queryKey: ["plex-libraries"],
    queryFn: api.plexLibraries,
    enabled,
  });
  const sync = useMutation({
    mutationFn: api.syncPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });

  // The ref makes this once-per-mount even though the mutation object's identity changes on
  // every render. `libraries.data` being an empty ARRAY is the trigger, not falsy: a failed
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
