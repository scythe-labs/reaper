// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Plex settings panel: link an account, pick which server and address Reaper uses,
// choose the libraries it may touch, and turn the "Leaving Soon" shelf on.
//
// Linking is optional. Scanning reads from Radarr and Sonarr, so everything here is about
// what Reaper may show and write *in Plex*. The sign-in itself is the shared PIN flow in
// PlexPin.tsx, the same one the login screen uses.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type RefObject, useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { api, type PlexLinkPoll, type PlexResourceConnection, type WatchEvidence } from "../api";
import { useSuccessorFocus } from "../focus";
import { count, since } from "../format";
import { useSafety } from "../useSafety";
import { ServerPickList, usePlexPinPoll } from "./PlexPin";
import { StaleReadNotice } from "./StaleReadNotice";
import { Switch } from "./Switch";
import { Notice } from "./Notice";

const MANUAL_CONNECTION = "__manual__";

/** The label a connection shows in the picker: where it goes, then how.
 *
 *  plex.direct hostnames embed the address as dashes ("192-168-20-73.abc….plex.direct"),
 *  which reads as noise; show the address itself and keep the certificate goodness as
 *  the "secure" tag. The full URI stays the option's value, so what is saved is exact. */
function connectionLabel(c: PlexResourceConnection): string {
  const kind = c.relay ? "Relay" : c.local ? "Local" : "Remote";
  let host = c.uri.replace(/^https?:\/\//, "");
  const direct = /^(\d+)-(\d+)-(\d+)-(\d+)\.[0-9a-f]+\.plex\.direct(:\d+)?$/i.exec(host);
  if (direct) {
    host = `${direct[1]}.${direct[2]}.${direct[3]}.${direct[4]}${direct[5] ?? ""}`;
  }
  const secure = c.protocol === "https" ? " · secure" : "";
  return `${kind} · ${host}${secure}`;
}

/** The address the Manual address row would save, or "" when it has no host to send. One
 *  declaration, so the Save that composes it and the dirty check that reports this draft up to
 *  `Settings` can never disagree about what the row is holding. */
function manualUri(host: string, port: string, ssl: boolean): string {
  const trimmed = host.trim();
  if (!trimmed) return "";
  return `${ssl ? "https" : "http"}://${trimmed}:${port.trim() || "32400"}`;
}

/** The three manual fields as a stored address seeds them: one parse, used both to FILL the row
 *  (`openManual`) and to say what "nothing typed" looks like (the dirty check below).
 *
 *  Both sides must go through this, because the stored string and the row's own composition of it
 *  are not the same text and comparing them directly reports edits nobody made (rule 39). Two
 *  ways that happens, each reachable by saving through this very box: `URL.hostname` lowercases,
 *  so a host typed with capitals comes back changed; and `URL.port` is empty for a scheme-default
 *  port, so the address has to be re-derived rather than guessed. Guessing 32400 for http was
 *  wrong twice over: it reported a phantom draft, and it showed :32400 for a server on :80 as
 *  though that were the current address. */
function seedManual(uri: string): { host: string; port: string; ssl: boolean } {
  try {
    const parsed = new URL(uri);
    const ssl = parsed.protocol === "https:";
    return { host: parsed.hostname, port: parsed.port || (ssl ? "443" : "80"), ssl };
  } catch {
    // No stored address to seed from: a fresh row on Plex's own default port.
    return { host: "", port: "32400", ssl: true };
  }
}

/** The watch-record status line: what Reaper holds, and what the last scan could not read.
 *
 *  `held_back === null` is "no scan has recorded it", which is NOT zero, so it says nothing at
 *  all rather than claiming none (rule 93). Zero is a real answer and is reported plainly,
 *  because "none" is exactly what tells an operator to leave this control alone.
 *
 *  **It reports what was COUNTED, never what was decided.** The stored number is items whose
 *  plays stopped being readable; nothing consults the verdict. Saying they were "held back" or
 *  "kept" would assert a protection this figure is not computed from: the hold comes from three
 *  gates the operator can each switch off, and with all three off the item is condemned exactly
 *  as before while this line still counted it (rule 144).
 *
 *  Nor "held back" as a phrase, whatever it claimed: this app has already bound that to an item
 *  with no readable size, in the planner and on four docs pages, where the repair is a policy
 *  allowance. Reusing it would send the operator there instead of to Tautulli.
 *
 *  It names the LAST SCAN rather than "right now" because that is where the number comes from:
 *  it does not move until the next scan runs, which is also why the reset leaves it standing. */
function watchEvidenceStatus(evidence: WatchEvidence): string {
  const titles = evidence.titles === 1 ? "1 title" : `${evidence.titles.toLocaleString()} titles`;
  if (evidence.held_back === null) return `Holding a record for ${titles}.`;
  if (evidence.held_back === 0) {
    return `Holding a record for ${titles}. The last scan found no unreadable plays.`;
  }
  const items =
    evidence.held_back === 1 ? "1 item" : `${evidence.held_back.toLocaleString()} items`;
  return `Holding a record for ${titles}. The last scan couldn't read the plays for ${items}.`;
}

export function PlexPanel({
  /** Called whenever this panel gains or loses an unsaved draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const queryClient = useQueryClient();
  const plex = useQuery({ queryKey: ["plex"], queryFn: api.plexStatus });
  const data = plex.data;
  const linked = data?.linked ?? false;
  const [linking, setLinking] = useState(false);
  // The plex.tv approval page opens in a new tab, but the click's popup permission is
  // already spent by the time the PIN comes back, so browsers often block it. Keep the
  // URL so the wait can offer it as a plain link, the way the login screen does.
  const [authUrl, setAuthUrl] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  // Failures get their own state so they render as an error, not as gray status text
  // that reads like "Linked to ...". Info stays in `message`.
  const [plexError, setPlexError] = useState<string | null>(null);
  // The two-step on the watch-record reset: armed, then how many the last press discarded.
  // A two-step in place rather than a native confirm(), which ignores the app's theme and
  // typography, and rather than a typed phrase, which this does not earn: it deletes no file
  // and the next scan rebuilds the record.
  //
  // The second step takes the admin password, the same credential and the same form as arming
  // deletion (`DeletionToggle`) and confirming a restore (`RestoreCard`). No file goes, but the
  // record is the only thing that can tell a title whose plays went unreadable apart from one
  // nobody ever watched, so discarding it withdraws that protection library-wide and the next
  // scan scores those titles as never watched. That is the condemn direction, which is what
  // earns the password here.
  const [forgetting, setForgetting] = useState(false);
  const [forgotten, setForgotten] = useState<number | null>(null);
  // Deliberately NOT part of `hasDrafts` below: a confirm credential is not work to lose, it is
  // three seconds of retyping, and `DeletionToggle` treats its own the same way. `onDirtyChange`
  // exists to stop the operator walking away from something they cannot get back.
  const [forgetPassword, setForgetPassword] = useState("");
  // The web-address box mirrors the saved value and follows it when a save (or another
  // tab) changes it; typing diverges the two until Save or a refetch reconciles them.
  const [webUrl, setWebUrl] = useState("");
  const [webUrlError, setWebUrlError] = useState<string | null>(null);
  // Both inline Saves on this panel exist only while their row is dirty, so pressing one destroys
  // it -- and each is `disabled` while its write is in flight, so focus is at `<body>` before that
  // (#173). Neither row has a heading of its own, so each hands focus to the control that outlives
  // it, and they are different controls: the web address box survives its own save and still holds
  // the value just committed, while the whole Manual address row collapses, leaving the Connection
  // select above it -- which is where the operator opened the row from, and which now reads back
  // the address they saved. Two hooks for the same reason they have two targets, plus two settle
  // timings: the web address waits for the `["plex"]` refetch, the manual row collapses inside
  // `onSuccess` and its select is `disabled` until the write clears.
  const afterWebUrlSave = useSuccessorFocus();
  const afterManualSave = useSuccessorFocus();
  const savedWebUrl = data?.web_url ?? "";
  // Which stored value the box currently holds a copy of. The effect below runs after the
  // commit, so the first pass where `data` exists paints an empty box against a stored
  // address and the dirty check reads a draft nobody typed -- the same one-frame report
  // `GeneralPanel` was making, and it reaches `Settings` through `onDirtyChange` the same way
  // (#139, rules 72 and 146). This panel re-seeds on EVERY change of the stored value rather
  // than once per load, so a bare "have we seeded" flag would not cover it: a save or another
  // tab moving the address opens the same window again. Asking which value the box was seeded
  // from closes both.
  //
  // `null`, never `savedWebUrl`: seeding the guard from the very value it has to DIFFER from
  // leaves it inert exactly where the bug bites. `["plex"]` is cached and `Settings` remounts
  // this panel on every section switch, so on a return visit `data` is already there on the
  // FIRST render -- `savedWebUrl` holds the stored address while the box is still "", the two
  // agree, and the panel reports a draft nobody typed. A sentinel no stored value can equal
  // (the shape `PolicyEditor` uses for its own draft) fails closed on that frame and still
  // re-seeds on every change after it.
  const [seededFrom, setSeededFrom] = useState<string | null>(null);
  useEffect(() => {
    setWebUrl(savedWebUrl);
    setSeededFrom(savedWebUrl);
  }, [savedWebUrl]);

  // The certificate check. Before linking it rides along with the link polls (so a
  // self-signed server can be reached at all); once linked it edits the stored server
  // row directly. The ref keeps the in-flight poll reading the current choice.
  const [verifyCert, setVerifyCert] = useState(true);
  const verifyRef = useRef(true);
  const savedVerify = data?.verify_tls ?? true;
  // The same sentinel as `seededFrom` above, and for the same reason (rule 72). Latent rather
  // than live: the status read omits the certificate flag while unlinked and the schema default
  // is on, so the stored value and this box's initial value are both `true` today and no warm
  // mount can catch them apart. That is a property of one server route, not of this guard, so
  // the guard is written to hold without it.
  const [verifySeededFrom, setVerifySeededFrom] = useState<boolean | null>(null);
  useEffect(() => {
    setVerifyCert(savedVerify);
    verifyRef.current = savedVerify;
    setVerifySeededFrom(savedVerify);
  }, [savedVerify]);

  // Every key that means "of the currently linked server". Declared up here, above the first
  // caller, because all three paths that change WHICH server that is have to run it: none of
  // these reads is qualified by a machine identifier, so a row cached against the old server
  // answers for the new one until its staleTime runs out (30s libraries, 60s resources).
  //
  // "Every" is grep-verified against the whole SPA, not counted from this file (rule 79). The
  // last key is the one that shows how little that claim is worth unchecked: it is read on the
  // Reap page, not this one, and it went unlisted while this comment said "the four reads
  // below" -- the four that happen to be declared underneath it. It costs the operator nothing
  // today, because a stale trash count can only ever over-warn, and it is listed anyway: the
  // next author reads "every" and stops grepping, which is exactly what happened here.
  const invalidateAllPlex = () => {
    void queryClient.invalidateQueries({ queryKey: ["plex"] });
    void queryClient.invalidateQueries({ queryKey: ["plex-resources"] });
    void queryClient.invalidateQueries({ queryKey: ["plex-libraries"] });
    void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] });
    void queryClient.invalidateQueries({ queryKey: ["plexTrash"] });
    // Both of its numbers are about the linked server: the marks are keyed on items only
    // this server holds, and the held-back count came from a scan of it.
    void queryClient.invalidateQueries({ queryKey: ["watch-evidence"] });
  };

  const saveWebUrl = useMutation({
    mutationFn: () => api.setPlexSettings({ web_url: webUrl.trim() }),
    // Re-seed from the response rather than leaving it to the effect above (rule 39). Clearing
    // the box is how the help says to go back to the hosted default, and it saves the empty
    // string: the route stores that as "unset" and reports back the SAME default string it was
    // already returning, so `savedWebUrl` never changes identity and that effect never re-fires.
    // The box would sit empty against a default it now matches -- a Save button that never goes
    // away, and, since this panel started reporting drafts upward, a section-switch confirm that
    // no button on this panel can satisfy, for a value that is already stored.
    onSuccess: (status) => {
      setWebUrl(status.web_url);
      setWebUrlError(null);
      // Its whole success signal was the Save button going away, which is an absence: the manual
      // address row beside it has said "Connection saved." all along, so this was the asymmetric
      // half of a pair (#173, rule 72).
      announce("Plex web address saved.");
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setWebUrlError(e.message),
  });

  // Flip the stored certificate check on the linked server. It sends the certificate check and
  // NOTHING else, which is the only way it cannot touch the address: it used to send
  // `savedWebUrl` beside it, so that the box's half-typed draft could not be saved by mistake --
  // but `savedWebUrl` is `["plex"]`'s cached row, and the route wrote whatever arrived. So a
  // switch about certificates wrote an address it has nothing to do with, and a cached row that
  // had gone out of date (a failed refetch this panel deliberately renders through, or another
  // tab editing the address) reverted the operator's address without a word, pointing every
  // "open in Plex" link in the app at plex.tv. The route keeps a field it was not sent (#204).
  const saveVerify = useMutation({
    mutationFn: (next: boolean) => api.setPlexSettings({ verify_tls: next }),
    onSuccess: () => {
      setPlexError(null);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    // The toggle flips optimiztically; a failed save must roll it back so the switch
    // never claims the certificate check is on while the server still has it off. The
    // switch is disabled while pending, so `!next` is the value before the flip.
    onError: (e: Error, next: boolean) => {
      setVerifyCert(!next);
      verifyRef.current = !next;
      setPlexError(e.message);
    },
  });

  // Ends a link attempt, however it ended. It refreshes the whole set rather than the status row
  // alone: linking is one of the three ways the linked server changes, so the library grid, the
  // server picker and the Leaving Soon settings all answer for a server that is no longer the one
  // on screen. Unlinking then linking a DIFFERENT server used to paint the previous server's
  // libraries and their enabled flags, and "Movies" and "TV Shows" collide across servers, so the
  // wrong list looked like the right one (#205).
  const done = () => {
    setLinking(false);
    invalidateAllPlex();
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  const pin = usePlexPinPoll<PlexLinkPoll>({
    poll: (pinId, machineId) => api.plexLinkPoll(pinId, machineId, verifyRef.current),
    onOk: (poll) => {
      const said = `Linked to ${poll.server?.name ?? "your server"}.`;
      setMessage(said);
      // `message` renders as a plain `.muted` paragraph, which is not a live region, and the
      // failure siblings below reach `Notice`'s `role="alert"` and so DO speak -- the same
      // success-is-silent asymmetry as the web address save above (#173, rule 72).
      announce(said);
      done();
    },
    // A poll, not a press. The picker replaces the "waiting for Plex" block on a timer, so there
    // is usually nothing focused to recover -- pressing "Link with Plex" already destroyed itself
    // one state earlier -- and moving focus off a timer is a steal rather than a recovery, which
    // is the line `useDialogFocus` draws for the same reason. So this site gets the news and NOT
    // a `.focus()`: the sign-in finished and there is now a choice waiting.
    onChooseServer: () =>
      announce("Signed in with Plex. Choose which server Reaper should manage."),
    // A sign-in that never completed is a failure, not status: it goes to `plexError`
    // so it renders as an error, not in the gray slot "Linked to ..." uses.
    onTimedOut: () => {
      setPlexError("Plex sign-in timed out. Try again.");
      done();
    },
    onFailed: (failure) => {
      setPlexError(failure);
      done();
    },
  });

  const startLink = async () => {
    setMessage(null);
    setPlexError(null);
    setAuthUrl("");
    setLinking(true);
    try {
      const start = await api.plexLinkStart();
      setAuthUrl(start.auth_url);
      window.open(start.auth_url, "_blank", "noopener");
      pin.begin(start.pin_id);
    } catch (e) {
      setPlexError(e instanceof Error ? e.message : String(e));
      setLinking(false);
    }
  };

  const cancelLink = () => {
    pin.cancel();
    setLinking(false);
    setAuthUrl("");
  };

  const cancelChoice = () => {
    setMessage(null);
    pin.cancel();
    done();
  };

  const unlink = useMutation({
    mutationFn: api.plexUnlink,
    // The same whole-set refresh `done` does, and for the same reason: after this there is no
    // linked server, so every row that meant "of the linked server" is about a server Reaper can
    // no longer reach (#205).
    onSuccess: () => {
      setPlexError(null);
      invalidateAllPlex();
      void queryClient.invalidateQueries({ queryKey: ["setup"] });
    },
    onError: (e: Error) => setPlexError(e.message),
  });

  // --- the server and connection pickers, fed by the signed-in account ---------

  const resources = useQuery({
    queryKey: ["plex-resources"],
    queryFn: api.plexResources,
    enabled: linked,
    staleTime: 60_000,
    retry: false,
  });

  const switchServer = useMutation({
    // Carry the operator's current certificate-check choice, so switching to a
    // self-signed server they have already turned it off for probes correctly.
    mutationFn: (machineId: string) => api.plexSwitchServer(machineId, verifyRef.current),
    onSuccess: (_result, machineId) => {
      // This one did not merely say nothing on success -- it CLEARED the only message slot on
      // the panel (below), so switching which server Reaper will delete from was quieter than
      // doing nothing. Named from the list rather than the id, falling back to a plain
      // sentence if the picker's own label cannot be resolved.
      const picked = resources.data?.servers.find((s) => s.machine_identifier === machineId);
      announce(picked ? `Now using ${picked.name}.` : "Server changed.");
      setMessage(null);
      setPlexError(null);
      invalidateAllPlex();
    },
    onError: (e: Error) => setPlexError(e.message),
  });

  const [manualOpen, setManualOpen] = useState(false);
  const [manualHost, setManualHost] = useState("");
  const [manualPort, setManualPort] = useState("32400");
  const [manualSsl, setManualSsl] = useState(true);
  const [connError, setConnError] = useState<string | null>(null);

  const setConnection = useMutation({
    mutationFn: (uri: string) => api.plexSetConnection(uri),
    onSuccess: () => {
      announce("Connection saved.");
      setConnError(null);
      // A successful connection save fixes reachability, so a prior "couldn't reach"
      // from a failed switch is now stale: clear it, or a red notice lingers beside a
      // healthy connection.
      setPlexError(null);
      // Collapsing the editor was the only success signal, and it unmounts the focused button.
      setManualOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setConnError(e.message),
  });

  // Only the server plex.tv marks `current` is the one Reaper is linked to. This used to fall
  // back to `servers[0]`, so a partial or filtered plex.tv response presented some OTHER server
  // as the managed one and the Connection row listed that server's addresses: picking one saved
  // it, and Reaper then wrote Leaving Soon collections and labels into, and read the Never-Reap
  // collection from, a server it was never linked to (B-10). Unknown stays unknown -- the row
  // says the list came back without it and both pickers go quiet until it is back.
  const currentServer = resources.data?.servers.find((s) => s.current);
  const linkedServerMissing = resources.data !== undefined && currentServer === undefined;
  const connections = currentServer?.connections ?? [];
  const savedUri = data?.connection_uri ?? "";
  const savedIsDiscovered = connections.some((c) => c.uri === savedUri);
  // A typed-in address keeps its own option value, so "Manual address…" is always a
  // different choice than the one already selected. Sharing one value meant picking it
  // fired no change event, and the editor could never be reopened.
  const connectionValue = manualOpen ? MANUAL_CONNECTION : savedUri;

  const openManual = () => {
    // Seed the manual fields from wherever Reaper is pointed right now.
    const seed = seedManual(savedUri);
    setManualHost(seed.host);
    setManualPort(seed.port);
    setManualSsl(seed.ssl);
    setConnError(null);
    setManualOpen(true);
  };

  const saveManual = () => {
    const uri = manualUri(manualHost, manualPort, manualSsl);
    if (!uri) return;
    setConnection.mutate(uri);
  };

  // --- libraries ---------------------------------------------------------------

  const libraries = useQuery({
    queryKey: ["plex-libraries"],
    queryFn: api.plexLibraries,
    enabled: linked,
  });
  const syncLibraries = useMutation({
    mutationFn: api.syncPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });
  const saveLibraries = useMutation({
    mutationFn: api.setPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });

  // First visit on a linked install: the list has never been synced, so fetch it once
  // rather than showing an empty grid with a button to press. The ref makes it
  // once-per-mount even though the mutation object's identity changes per render.
  const autoSynced = useRef(false);
  useEffect(() => {
    if (linked && libraries.data && libraries.data.length === 0 && !autoSynced.current) {
      autoSynced.current = true;
      syncLibraries.mutate();
    }
  }, [linked, libraries.data, syncLibraries]);

  const toggleLibrary = (key: number, next: boolean) => {
    const libs = libraries.data ?? [];
    const enabled = new Set(libs.filter((l) => l.enabled).map((l) => l.key));
    if (next) enabled.add(key);
    else enabled.delete(key);
    saveLibraries.mutate([...enabled]);
  };

  // --- recorded watch history ----------------------------------------------------

  const watchEvidence = useQuery({
    queryKey: ["watch-evidence"],
    queryFn: api.watchEvidence,
    enabled: linked,
  });
  // Whether an admin password exists at all. Read from `useSafety` rather than added to
  // `/watch-evidence`, because the answer already ships on `/safety` and every surface that
  // gates on it reads it there: a second copy of one fact is the copy that goes wrong (rule
  // 144). It is the same flag `DeletionToggle` branches on for the same reason.
  const safety = useSafety();
  const forgetWatchEvidence = useMutation({
    mutationFn: api.resetWatchEvidence,
    // The typed password is dropped on the way out on BOTH exits, success and refusal alike.
    // Holding it would leave the admin password sitting in component state for as long as the
    // panel stays mounted and refill the box on the next open, which is the shape S-5 was filed
    // for; `RestoreCard` clears on a refusal for the same reason.
    onError: () => setForgetPassword(""),
    onSuccess: (result) => {
      setForgetting(false);
      setForgetPassword("");
      setForgotten(result.forgotten);
      // Refetch rather than patch: the reset moves `titles` to zero, but `held_back` is a
      // property of the LAST SCAN and does not change until the next one runs. Writing the
      // pair by hand here would have to restate that, and would be the place it drifts.
      void queryClient.invalidateQueries({ queryKey: ["watch-evidence"] });
    },
  });

  // --- Leaving Soon --------------------------------------------------------------

  const leavingSoon = useQuery({
    queryKey: ["leaving-soon-settings"],
    queryFn: api.leavingSoonSettings,
    enabled: linked,
  });
  const saveLeavingSoon = useMutation({
    mutationFn: api.setLeavingSoonSettings,
    onSuccess: (s) => {
      queryClient.setQueryData(["leaving-soon-settings"], s);
    },
  });

  const lsStatus = (() => {
    if (!leavingSoon.data) return null;
    const last = leavingSoon.data.last;
    if (!last) return "Not updated yet. It runs after every scan, or from the Jobs page.";
    const movies = `${count(last.movies)} movie${last.movies === 1 ? "" : "s"}`;
    const seasons = `${count(last.seasons)} season${last.seasons === 1 ? "" : "s"}`;
    const wrote = last.applied ? "" : " · preview only, nothing was written in Plex";
    return `Last updated ${since(last.at)} · ${movies} and ${seasons} on the shelves · next update after the next scan${wrote}`;
  })();

  // What this panel would LOSE, reported up to `Settings` so leaving the section can stop and ask
  // first. Declared above the early returns below because the effect carrying it is a hook, and
  // because rule 146 asks what the parent is told in each of those states as well as in the form.
  //
  // Each draft is tested against the exact condition that RENDERS the row holding it, because the
  // report makes two claims at once -- there is something to lose, AND the operator can still get
  // to it. A guard that keeps the first claim after the second has gone false is a trap: it
  // demands a discard for a box that is no longer on screen, and the only exit it leaves is the
  // destructive button.
  const webUrlDirty = seededFrom === savedWebUrl && webUrl.trim() !== savedWebUrl;
  // This exact value renders the row's Save (below), so the report and the button agree by
  // sharing one const rather than by two copies of one expression staying in step. They were
  // two copies, and adding the seed guard to this one alone made this comment false the same
  // day it was written: the button appeared beside an unseeded empty box while the panel
  // reported nothing to lose (rule 7/24).
  //
  // The manual row is behind `linked && manualOpen`, so both belong in its claim: unlinking
  // leaves `manualOpen` set while the row is gone. An empty host is not a draft to lose -- Save
  // is disabled without one -- and neither is an address that already matches the stored one.
  // Both sides of that last test go through `seedManual` + `manualUri`, so "I only opened the
  // row" is non-dirty by construction rather than by luck with the URI's spelling (rule 39).
  const manualDraft = manualUri(manualHost, manualPort, manualSsl);
  const savedSeed = seedManual(savedUri);
  const savedManual = manualUri(savedSeed.host, savedSeed.port, savedSeed.ssl);
  const manualDirty = linked && manualOpen && manualDraft !== "" && manualDraft !== savedManual;
  // The certificate switch writes on the spot ONLY once a server is linked -- its onChange below
  // is `if (linked) saveVerify.mutate(next)`. Before that the choice lives here and rides along
  // with the link poll through `verifyRef`, so leaving the section silently drops it and the next
  // sign-in probes a self-signed server with checking back on, failing with nothing on screen to
  // explain it. Its row renders unconditionally, so the reachability half of the claim holds
  // wherever this form does. Once linked the switch is not a draft at all: it is already saved.
  const certDirty = !linked && verifySeededFrom === savedVerify && verifyCert !== savedVerify;
  const hasDrafts = webUrlDirty || manualDirty || certDirty;
  useEffect(() => {
    onDirtyChange?.(hasDrafts);
  }, [hasDrafts, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Until the status has actually been read, nothing here may claim a state: an unread
  // query looks exactly like "not linked", and that would invite a needless re-link
  // through the whole Plex sign-in over a momentary hiccup. Nothing is dirty here either:
  // `data` is undefined, so the web-address box and its saved value are both "".
  if (plex.isPending) {
    return (
      <div className="panel">
        <h2>Plex</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  }
  // Only when there is nothing to render, the same test the General panel settled on (rule 72).
  // This used to fire on `plex.isError` too, which meant a refetch that failed AFTER a good load
  // traded the whole form for this paragraph while React Query still held the last good row --
  // taking the web-address box and its Save with it while the typed value stayed in state, still
  // reported unsaved to `Settings`, which then asked to discard an edit the operator could no
  // longer see or put back. Saving the certificate switch is enough to reach it, because that
  // invalidates this very query. So a failed refetch keeps the form on the last good values.
  if (!data) {
    return (
      <div className="panel">
        <h2>Plex</h2>
        <Notice tone="error">Couldn't load these settings. Reload to try again.</Notice>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2>Plex</h2>
      <p className="blurb">
        Linking Plex lets Reaper warn your library with a "Leaving Soon" shelf and read your "Never
        Reap" collection. It's optional. Scanning works without it.
      </p>

      {/* The failed refetch the `!data` branch above deliberately no longer swallows the form
          for. Keeping the form is right; keeping it SILENTLY is not, because everything below
          then reads as current when it is known to be stale (rule 17/36). */}
      {plex.isError && <StaleReadNotice />}

      <div className="set-group">
        <h3>Connection</h3>
        <div className="set-rows">
          {linked && data ? (
            /* One Unlink button, not a box, so it releases the control track
               (`.set-row-plain`). */
            <div className="set-row set-row-plain">
              {/* Lead with the person signed in, not the server name (that lives one row down
                  in the Server picker). The account name is live from plex.tv, which always
                  resolves after the fast, local-only status query above, so show a neutral
                  placeholder while it's in flight rather than flashing the server name; only
                  fall back to the server name once resources has actually settled without a
                  username (plex.tv unreachable). */}
              <span className="set-label">
                {resources.isPending ? "Loading…" : (resources.data?.owner_username ?? data.name)}
              </span>
              <p className="help">Signed in with Plex. {data.connection_uri}</p>
              <div className="set-control">
                <button
                  className="ghost"
                  onClick={() => unlink.mutate()}
                  disabled={unlink.isPending}
                >
                  Unlink
                </button>
              </div>
            </div>
          ) : pin.servers ? (
            <div className="set-row">
              <span className="set-label">Which server should Reaper manage?</span>
              <p className="help">
                This account owns more than one Plex server. Reaper will only ever scan and prune
                the one you pick.
              </p>
              <div className="set-control server-pick">
                <ServerPickList
                  servers={pin.servers}
                  onPick={(machineId) => void pin.pick(machineId)}
                  onCancel={cancelChoice}
                />
              </div>
            </div>
          ) : (
            <div className="set-row">
              <span className="set-label">No Plex server linked</span>
              <p className="help">
                Sign in with Plex and Reaper discovers your servers. It never asks for a token by
                hand.
              </p>
              <div className="set-control">
                {linking ? (
                  // The same wait the login screen shows, worded the same: a fallback link
                  // for a blocked popup, and a way out that stops the polling.
                  <div className="plex-waiting">
                    <span className="spinner" aria-hidden="true" />
                    <div>
                      <strong>Waiting for Plex…</strong>
                      {/* Once the sign-in is approved, the wait can continue for a second
                          reason: the server itself isn't answering yet. Say which one it
                          is, so a longer wait doesn't read as a hang. Reaper keeps
                          polling either way; the sign-in stays good. */}
                      <p className="muted">
                        {pin.retrying ?? (
                          <>
                            Approve the sign-in in the Plex window.{" "}
                            {authUrl !== "" && (
                              <a href={authUrl} target="_blank" rel="noreferrer">
                                Didn’t open?
                              </a>
                            )}
                          </>
                        )}
                      </p>
                    </div>
                    <button className="link" onClick={cancelLink}>
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button className="btn-plex" onClick={startLink}>
                    Link with Plex
                  </button>
                )}
              </div>
            </div>
          )}

          {linked && (
            <div className="set-row">
              <span className="set-label">Server</span>
              <p className="help">
                Plex servers this account can manage. Reaper works with one at a time.
                {resources.data?.source === "stored" &&
                  " Showing what was remembered at link time; plex.tv didn't answer."}
              </p>
              <div className="set-control">
                {resources.isPending ? (
                  <span className="muted">Looking for servers…</span>
                ) : resources.isError ? (
                  <>
                    <span className="muted">Couldn't list this account's servers.</span>
                    <button className="ghost sm" onClick={() => void resources.refetch()}>
                      Retry
                    </button>
                  </>
                ) : (
                  <>
                    {linkedServerMissing && (
                      <Notice tone="warn">
                        Plex's list came back without the server Reaper uses
                        {data?.name ? `, ${data.name}` : ""}. Nothing has changed. Refresh to look
                        again; the server and connection stay as they are until it is back.
                      </Notice>
                    )}
                    <select
                      value={currentServer?.machine_identifier ?? ""}
                      aria-label="Server"
                      disabled={switchServer.isPending || linkedServerMissing}
                      onChange={(e) => {
                        const next = e.target.value;
                        if (next && next !== currentServer?.machine_identifier) {
                          switchServer.mutate(next);
                        }
                      }}
                    >
                      {linkedServerMissing ? (
                        // A select whose value matches no option displays its FIRST option, so
                        // listing the others here would still show one of them as the current
                        // server -- the exact misreading this fix exists to stop, merely no
                        // longer savable. The box names what Reaper actually uses instead.
                        <option value="">{data?.name ?? "The linked server"}</option>
                      ) : (
                        (resources.data?.servers ?? []).map((s) => (
                          <option key={s.machine_identifier} value={s.machine_identifier}>
                            {s.name}
                          </option>
                        ))
                      )}
                    </select>
                    <button
                      className="ghost sm"
                      disabled={resources.isFetching}
                      onClick={() => void resources.refetch()}
                      title="Look for servers again"
                    >
                      {resources.isFetching ? "Refreshing…" : "Refresh"}
                    </button>
                  </>
                )}
              </div>
            </div>
          )}

          {linked && (
            <div className="set-row">
              <span className="set-label">Connection</span>
              <p className="help">
                How Reaper reaches the server. A local address is usually faster; remote works from
                anywhere. Pick "Manual address" to type your own.
              </p>
              <div className="set-control">
                <select
                  ref={afterManualSave.ref as RefObject<HTMLSelectElement>}
                  value={connectionValue}
                  aria-label="Connection"
                  // Without the linked server there is nothing to list but the saved address,
                  // and every choice here would point at another server's addresses (B-10).
                  disabled={setConnection.isPending || resources.isPending || linkedServerMissing}
                  onChange={(e) => {
                    const next = e.target.value;
                    if (next === MANUAL_CONNECTION) openManual();
                    else {
                      setManualOpen(false);
                      if (next !== savedUri) setConnection.mutate(next);
                    }
                  }}
                >
                  {connections.map((c) => (
                    <option key={c.uri} value={c.uri}>
                      {connectionLabel(c)}
                    </option>
                  ))}
                  {!savedIsDiscovered && savedUri !== "" && (
                    <option value={savedUri}>Manual · {savedUri}</option>
                  )}
                  <option value={MANUAL_CONNECTION}>Manual address…</option>
                </select>
              </div>
            </div>
          )}

          {linked && manualOpen && (
            // A cluster, not a box: host, port, an SSL switch and Save belong on one line, so
            // this row keeps the shrink-to-fit control column (see `.set-row-cluster`).
            <div className="set-row set-row-cluster">
              <span className="set-label">Manual address</span>
              <p className="help">Hostname or IP, port, and whether to use SSL.</p>
              <div className="set-control">
                {/* Two boxes under one `.set-label`, named the way the accent row names its
                    pair: the row's label, then which half this is. A placeholder is a name of
                    last resort, so without these the host box announced itself as the example
                    address in it. */}
                <input
                  type="text"
                  className="input-host"
                  value={manualHost}
                  aria-label="Manual address host or IP"
                  onChange={(e) => setManualHost(e.target.value)}
                  placeholder="plex.example.net"
                  autoComplete="off"
                />
                <input
                  type="text"
                  className="input-port"
                  value={manualPort}
                  aria-label="Manual address port"
                  onChange={(e) => setManualPort(e.target.value.replace(/\D/g, ""))}
                  placeholder="32400"
                  inputMode="numeric"
                />
                <label className="toggle" title="Use SSL">
                  <Switch checked={manualSsl} onChange={setManualSsl} ariaLabel="Use SSL" />
                  <span>SSL</span>
                </label>
                <button
                  className="primary sm"
                  disabled={!manualHost.trim() || setConnection.isPending}
                  onClick={() => {
                    afterManualSave.arriving();
                    saveManual();
                  }}
                >
                  {setConnection.isPending ? "Checking…" : "Save"}
                </button>
              </div>
            </div>
          )}

          {/* A Switch, not a box, so it releases the control track (`.set-row-plain`). */}
          <div className="set-row set-row-plain">
            <span className="set-label">Check the server's certificate</span>
            <p className="help">
              Turn this off only for a server you run yourself, like one with a self-signed
              certificate.
            </p>
            <div className="set-control">
              <Switch
                checked={verifyCert}
                disabled={saveVerify.isPending}
                ariaLabel="Check the server's certificate"
                onChange={(next) => {
                  setVerifyCert(next);
                  verifyRef.current = next;
                  if (linked) saveVerify.mutate(next);
                }}
              />
            </div>
            {!verifyCert && (
              <Notice tone="warn">
                Reaper will accept this server's certificate without checking who issued it.
              </Notice>
            )}
          </div>

          <div className="set-row">
            <span className="set-label">Plex web address</span>
            <p className="help">
              Where links to your library open. Keep the default unless you host your own Plex Web.
              Clear it and save to go back to the default.
            </p>
            {/* This row and Manual address above keep their own inline Save, where the General
                panel now has one save bar for the whole panel (rule 43). Deferred on purpose,
                not missed: these two save through different routes than `saveGeneral`, so
                collecting them costs a shared draft model this change does not need. What DID
                have to land on them is the reflow behind it (rule 72), and it has: the box
                grows into the track and gives the width back from the right, so its left edge
                and its text hold still as this button appears. The section-switch confirm has
                landed on them too, through `onDirtyChange` above -- a panel need not have a save
                bar to be asked about before it is unmounted. Whoever gives Plex a save bar takes
                both rows together. */}
            <div className="set-control">
              <input
                type="url"
                ref={afterWebUrlSave.ref as RefObject<HTMLInputElement>}
                value={webUrl}
                aria-label="Plex web address"
                onChange={(e) => {
                  setWebUrl(e.target.value);
                  setWebUrlError(null);
                }}
                placeholder="https://app.plex.tv"
                autoComplete="off"
              />
              {/* `webUrlDirty` above, not a second copy of its comparison: the report and this
                  button are one claim, and they were two expressions until one of them grew a
                  seed guard. */}
              {webUrlDirty && (
                <button
                  type="button"
                  className="primary sm"
                  disabled={saveWebUrl.isPending}
                  onClick={() => {
                    afterWebUrlSave.arriving();
                    saveWebUrl.mutate();
                  }}
                >
                  {saveWebUrl.isPending ? "Saving…" : "Save"}
                </button>
              )}
            </div>
          </div>
        </div>

        {connError && <Notice tone="error">{connError}</Notice>}
        {webUrlError && <Notice tone="error">{webUrlError}</Notice>}
        {plexError && <Notice tone="error">{plexError}</Notice>}
        {message && <p className="muted">{message}</p>}
      </div>

      {linked && (
        <div className="set-group">
          <h3>Libraries</h3>
          <p className="group-blurb">
            The libraries Reaper may touch in Plex. Leaving Soon shelves are managed only in
            libraries you turn on. This doesn't change what gets scanned: scanning reads from Radarr
            and Sonarr.
          </p>
          {/* Only when there is nothing to render, the same divided test the panel's own status
              read settled on above (rule 72). An undivided `isError` traded the whole grid, every
              per-library Switch and the Refresh button for one paragraph while React Query still
              held the last good list (#166).

              Ordinary things reach it. `invalidateAllPlex` invalidates this key, and all three
              paths that change which server is linked now call it: `switchServer.onSuccess`,
              `done` (the link path) and `unlink.onSuccess`. For most of this comment's life it
              had exactly one caller and the other two invalidated the status row alone, so the
              grid answered for the previous server (#205); an earlier comment named all three
              anyway, which #196 corrected down to the one that was true at the time. `SetupWizard`
              reaches it by a second route: it fires a bare `invalidateQueries()` when the first
              scan ends, and it renders this panel on that same screen, so every key refetches
              with the last good list in hand.

              Which arm a LINK lands on depends on whether this key has ever been read. On a
              fresh install it has not -- the query is `enabled: linked`, so linking mounts it
              for the first time and a failure there is the never-loaded arm above. After an
              unlink it has: invalidating marks the entry stale but keeps it, and this observer
              stays mounted, so linking a different server refetches with the PREVIOUS server's
              list still in hand and a failure lands here, on the stale arm, with the notice up.
              Shorter and quieter: between the link landing and the refetch resolving, neither
              `isPending` nor `isError` is set, so that list renders unmarked for a moment. Both
              are bounded -- `set_plex_libraries` walks the server's own stored list and ignores
              keys it does not contain (#194), so a press cannot mis-target -- and the earlier
              draft of this paragraph denied the state exists at all, which is the part that
              would have stopped the next reader looking.

              The list is a read, not a draft, so what is lost is smaller than the General panel's
              typed value -- but a switch the operator cannot see is a library they cannot turn
              off. */}
          {libraries.isPending || syncLibraries.isPending ? (
            <p className="muted">Loading libraries…</p>
          ) : libraries.isError && !libraries.data ? (
            <Notice tone="error">Couldn't load the library list.</Notice>
          ) : (
            <>
              {libraries.isError && <StaleReadNotice what="the library list" />}
              <div className="lib-head">
                <span className="muted">
                  {count((libraries.data ?? []).length)}{" "}
                  {(libraries.data ?? []).length === 1 ? "library" : "libraries"} found
                </span>
                <button
                  className="ghost sm"
                  disabled={syncLibraries.isPending}
                  onClick={() => syncLibraries.mutate()}
                >
                  Refresh libraries
                </button>
              </div>
              <div className="lib-grid">
                {(libraries.data ?? []).map((lib) => (
                  <div key={lib.key} className={lib.enabled ? "lib-card" : "lib-card off"}>
                    <span>
                      {lib.title}
                      <span className="lib-kind">{lib.kind === "movie" ? "movies" : "tv"}</span>
                    </span>
                    <Switch
                      checked={lib.enabled}
                      disabled={saveLibraries.isPending}
                      ariaLabel={`Let Reaper touch ${lib.title}`}
                      onChange={(next) => toggleLibrary(lib.key, next)}
                    />
                  </div>
                ))}
              </div>
              {(saveLibraries.error || syncLibraries.error) && (
                <Notice tone="error">
                  {(saveLibraries.error ?? syncLibraries.error)?.message}
                </Notice>
              )}
            </>
          )}
        </div>
      )}

      {linked && (
        <div className="set-group">
          <h3>Watch history</h3>
          <p className="group-blurb">
            Plex gives every item a number, and issues a new one when a file leaves a library and
            comes back. Plays recorded before that stay under the old number.
          </p>
          {/* Same shape as the two groups around it: a failed read keeps the last good answer
              on screen and says so, rather than blanking a control the operator came here for
              (rule 17/36, and rule 72 with the library grid and the shelf switches). */}
          {watchEvidence.isPending ? (
            <p className="muted">Loading…</p>
          ) : !watchEvidence.data ? (
            <Notice tone="error">Couldn't load the watch history record.</Notice>
          ) : (
            <>
              {watchEvidence.isError && <StaleReadNotice what="the watch history record" />}
              <div className="set-rows">
                <div className="set-row set-row-plain">
                  <span className="set-label">Recorded watch history</span>
                  <p className="help">
                    Reaper holds back a title whose plays stop being readable. After a library
                    rebuild that can be every title at once. Forget the record to start from what
                    Plex holds now.
                  </p>
                  {/* Five states, all explicit (rule 17/36). The three that are not the form
                      all fail closed: this control withdraws a protection, so a safety read
                      Reaper could not complete offers nothing to press rather than assuming a
                      password is there to check against. `DeletionToggle` splits the same way,
                      and keeps its OFF direction live on an unreadable state because that
                      direction can only make Reaper safer -- this one has no such direction,
                      which is why unknown ends the branch here. */}
                  <div className="set-control">
                    {safety.isLoading ? (
                      <span className="muted">Checking…</span>
                    ) : !safety.data ? (
                      <span className="muted">
                        Reaper couldn't check whether an admin password is set.
                      </span>
                    ) : !safety.data.has_password ? (
                      <span className="muted">
                        Set an admin password first, in Settings → Security.
                      </span>
                    ) : forgetting ? (
                      /* The same form as arming deletion: one password box, Confirm, Cancel.
                         The placeholder is a hint that disappears on the first keystroke, so
                         the field is named by its label either way.
                         `pw-inline` because this one sits in a settings ROW, not the Security
                         panel's field pane: `.pw-form` alone is a column, which put the box and
                         BOTH buttons at full width on a phone. */
                      <form
                        className="pw-form pw-inline"
                        onSubmit={(e) => {
                          e.preventDefault();
                          forgetWatchEvidence.mutate(forgetPassword);
                        }}
                      >
                        <input
                          type="password"
                          value={forgetPassword}
                          onChange={(e) => setForgetPassword(e.target.value)}
                          maxLength={128}
                          placeholder="admin password"
                          aria-label="Admin password"
                          autoComplete="current-password"
                          autoFocus
                        />
                        {/* `danger` and a plain button, the pair this row already had before the
                            password box joined them. Not `sm`, which the arming form spells but
                            no stylesheet defines: an inert class reads as a size that was chosen
                            (rule 18). Left in place there rather than swept, since removing a
                            no-op moves no pixel. */}
                        <button
                          type="submit"
                          className="danger"
                          disabled={!forgetPassword || forgetWatchEvidence.isPending}
                        >
                          Confirm forget
                        </button>
                        {/* Cancel drops the typed password with the form, same as the arming
                            form's own Cancel (S-5). */}
                        <button
                          type="button"
                          disabled={forgetWatchEvidence.isPending}
                          onClick={() => {
                            setForgetting(false);
                            setForgetPassword("");
                          }}
                        >
                          Cancel
                        </button>
                      </form>
                    ) : (
                      <button
                        className="ghost"
                        title="Reaper starts from what Plex holds now"
                        onClick={() => {
                          setForgotten(null);
                          setForgetting(true);
                        }}
                      >
                        Forget…
                      </button>
                    )}
                  </div>
                </div>
                <div className="set-row set-status">
                  {/* Says when it takes effect, because nothing on any surface moves until the
                      next scan: the record is gone, but the stored candidates and their facts
                      are frozen snapshot data. Without the second sentence an operator reads
                      the unchanged queue as "that did not work" and reaches for the policy
                      next, which does cost files. Same clause as the restore notice and the
                      shelf hint (rule 72). */}
                  {forgotten !== null
                    ? `Forgotten for ${forgotten.toLocaleString()} ${
                        forgotten === 1 ? "title" : "titles"
                      }. The next scan uses what Plex holds now.`
                    : watchEvidenceStatus(watchEvidence.data)}
                </div>
              </div>
              {/* The server's own sentence, not a fixed one. Every refusal this can now meet
                  says something the operator has to act on differently -- a password that did
                  not match, one not set yet, too many tries -- and one sentence covering all
                  of them would name none of them. Each already reads as plain language and
                  already states that the record was kept, so there is no lead-in either: that
                  would print the reassurance twice. `DeletionToggle` renders its arming
                  failures the same way (rule 72). */}
              {forgetWatchEvidence.error && (
                <Notice tone="error">
                  {forgetWatchEvidence.error.message ||
                    "Couldn't forget the record. Nothing changed. Try again."}
                </Notice>
              )}
              {/* `standing`: it is always on screen, so it must not announce itself as an
                  alert every time this panel renders. */}
              <Notice tone="warn" standing>
                A title that really was watched will score as never watched until you repair its
                history in Tautulli.
              </Notice>
              <p className="group-hint muted">
                Tautulli only moves old plays to the new number when you use its Fix Metadata
                screen.
              </p>
            </>
          )}
        </div>
      )}

      {linked && (
        <div className="set-group">
          <h3>Leaving Soon</h3>
          <p className="group-blurb">
            While an item counts down its grace period, Reaper can put it on a "Leaving Soon" shelf
            in Plex, so people get a heads-up before it goes: movies in your movie libraries,
            seasons in your TV libraries.
          </p>
          {/* `!leavingSoon.data` alone: a read that never landed leaves it undefined and still
              reaches the sentence below, so dropping the `isError ||` in front of it costs nothing
              there and stops a failed REFETCH taking both switches off screen while the last good
              answer is still held (#166, rule 72 with the library grid above). These two are the
              shelf's on/off and its read-only override -- the switch that decides whether Reaper
              writes to Plex before deletion is armed -- so blanking them hides a control the
              operator came here to set. */}
          {leavingSoon.isPending ? (
            <p className="muted">Loading…</p>
          ) : !leavingSoon.data ? (
            <Notice tone="error">Couldn't load the Leaving Soon settings.</Notice>
          ) : (
            <>
              {leavingSoon.isError && <StaleReadNotice what="the Leaving Soon settings" />}
              <div className="set-rows">
                {/* Both rows here carry a Switch and nothing else, so they release the control
                  track (`.set-row-plain`). */}
                <div className="set-row set-row-plain">
                  <span className="set-label">Show "Leaving Soon" in Plex</span>
                  <p className="help">
                    Reaper keeps a Leaving Soon collection in each library you turned on above, and
                    puts the matching label on everything in it. Items appear when they start
                    counting down and drop off when they're spared or removed. Updates after every
                    scan, or from the Jobs page.
                  </p>
                  <div className="set-control">
                    <Switch
                      checked={leavingSoon.data.enabled}
                      disabled={saveLeavingSoon.isPending}
                      ariaLabel='Show "Leaving Soon" in Plex'
                      onChange={(enabled) => saveLeavingSoon.mutate({ enabled })}
                    />
                  </div>
                </div>
                <div className="set-row set-row-plain">
                  <span className="set-label">Update while read-only</span>
                  <p className="help">
                    Until deletion is on, Reaper writes nothing to Plex, including this shelf. Turn
                    this on to let the warning appear while Reaper is still read-only. It can only
                    manage the collection and label. It can never remove files.
                  </p>
                  <div className="set-control">
                    <Switch
                      checked={leavingSoon.data.allow_unarmed}
                      disabled={saveLeavingSoon.isPending}
                      ariaLabel="Update while read-only"
                      onChange={(allow_unarmed) => saveLeavingSoon.mutate({ allow_unarmed })}
                    />
                  </div>
                </div>
                {lsStatus && (
                  <div className="set-row set-status">
                    <span>{lsStatus}</span>
                  </div>
                )}
              </div>
            </>
          )}
          {saveLeavingSoon.error && <Notice tone="error">{saveLeavingSoon.error.message}</Notice>}
        </div>
      )}

      {!linked && (
        <p className="help">Link Plex to pick libraries and turn on the "Leaving Soon" shelf.</p>
      )}
    </div>
  );
}
