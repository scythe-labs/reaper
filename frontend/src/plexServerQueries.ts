import type { QueryClient } from "@tanstack/react-query";

/** Every query key that means "of the currently linked Plex server".
 *
 *  None of these reads is qualified by a machine identifier, so a row cached against the old
 *  server answers for the new one until its staleTime runs out (30s libraries, 60s resources).
 *
 *  **"Every" is grep-verified against the whole SPA, not read off any one component** (rule 79).
 *  `["plexTrash"]` is the key that shows how little the claim is worth unchecked: it is read on
 *  the Reap page, and it went unlisted while the comment that used to hold this list said "the
 *  four reads below" -- the four that happened to be declared underneath it.
 */
export const OF_THE_LINKED_SERVER = [
  ["plex"],
  ["plex-resources"],
  ["plex-libraries"],
  ["leaving-soon-settings"],
  ["plexTrash"],
  // Both of its numbers are about the linked server: the marks are keyed on items only this
  // server holds, and the held-back count came from a scan of it.
  ["watch-evidence"],
] as const;

/** Stop trusting every row that meant "of the linked server".
 *
 *  **Called by every path that changes WHICH server is linked, and there are five in two
 *  components**: `PlexPanel`'s link, unlink and switch, and `SetupPlexStep`'s link and switch.
 *  It lives here rather than inside `PlexPanel` because it used to, and the wizard then
 *  open-coded a three-key subset of it -- so a key added to the panel's copy reached three of
 *  the five paths and the claim on the other two silently became false (rule 144).
 *
 *  `["setup"]` is deliberately not here. It is not about the linked server, only some of these
 *  callers need it, and folding it in would make this helper's own count disagree with the set
 *  it names. Callers invalidate it themselves.
 *
 *  **Saving a connection is not one of the five.** It changes the ADDRESS of the same server,
 *  so every row about that server is still about it; both `setConnection` mutations invalidate
 *  `["plex"]` alone, and that is correct rather than an omission.
 */
export function invalidateAllPlex(queryClient: QueryClient): void {
  for (const queryKey of OF_THE_LINKED_SERVER) {
    void queryClient.invalidateQueries({ queryKey: [...queryKey] });
  }
}
