// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one query behind every update surface -- the About row and pill, the account
// chip's light, the dropdown's item -- so they cannot disagree about whether an
// update exists. The server holds its own answer for hours (GitHub's anonymous API
// allows 60 requests an hour for the whole host); staleTime here keeps a Settings
// remount from asking the server again anyway.

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

export function useUpdateStatus() {
  return useQuery({
    queryKey: ["update"],
    queryFn: api.update,
    staleTime: 30 * 60 * 1000,
  });
}
