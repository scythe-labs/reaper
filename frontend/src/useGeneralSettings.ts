// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one declaration of the general-settings read.
//
// Freshness is one decision for a value the settings panel writes and four other surfaces
// render, so it is made once here, and it is the app-wide default in `main.tsx` rather than
// a number spelled again at each call site.

import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, type GeneralSettings } from "./api";
import { preferredLanguage } from "./i18n";

/** The install's General settings.
 *
 *  `subscribed: false` for a caller that already has the values from somewhere else: the query
 *  then neither fetches nor re-renders its component when the cache is written. `enabled` alone
 *  stops only the fetch, leaving every holder subscribed to every write. That is the
 *  thousand-observer fan-out `components/queueSettings.tsx`'s provider exists to remove, and
 *  `useDefaultSpareDays` is the one caller passing false. */
export function useGeneralSettings(subscribed = true): UseQueryResult<GeneralSettings> {
  return useQuery({
    queryKey: ["general-settings"],
    queryFn: api.general,
    enabled: subscribed,
    ...(subscribed ? {} : { notifyOnChangeProps: [] as [] }),
  });
}

/** Write this browser's language to the server the first time it finds none stored there.
 *
 *  The language lives on the server because a notification is composed there, with no browser
 *  to ask. But the server cannot detect a language on its own, and the first authenticated
 *  read is the earliest moment a browser that can is talking to it. `AuthGuard` opens only
 *  `/api/health` and `/api/auth/`, so nothing before sign-in could carry the answer.
 *
 *  It runs on every install rather than only at the end of the setup wizard, because an
 *  existing install never runs the wizard again. Without it, the picker in Settings -> General
 *  would show one language while a notification was written in another.
 *
 *  Fires once per page: `sent` latches before the request rather than after, so a re-render
 *  while it is in flight cannot start a second one. */
export function useSeedLanguage(): void {
  const queryClient = useQueryClient();
  const { data } = useGeneralSettings();
  const sent = useRef(false);
  useEffect(() => {
    if (!data || data.language !== null || sent.current) return;
    sent.current = true;
    void api
      .saveGeneral({ language: preferredLanguage() })
      .then((saved) => queryClient.setQueryData(["general-settings"], saved))
      // A seed that cannot be written is not worth a screen: the app is already painting in
      // this language, and the operator can still set it in Settings. The next load retries.
      .catch(() => {});
  }, [data, queryClient]);
}
