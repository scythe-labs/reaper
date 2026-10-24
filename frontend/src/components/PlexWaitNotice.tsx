// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The amber notice shown while Reaper waits for Plex to finish the folder scans a reap asked
// for. Standing page furniture, so it is a status region and not an alert.

import type { ReactNode } from "react";

export function PlexWaitNotice({ children }: { children: ReactNode }) {
  return (
    <div className="banner banner-unknown" role="status">
      <span className="banner-dot" aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}
