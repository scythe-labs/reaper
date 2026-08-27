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
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type PlexLinkPoll, type WatchEvidence } from "../api";
import { describeError } from "../errors";
import { useSuccessorFocus } from "../focus";
import i18next from "../i18n";
import { countBesideServerText, since } from "../format";
import { invalidateAllPlex as invalidateAllPlexQueries } from "../plexServerQueries";
import { shelfRenamePending, shelfSkipIsCurrent } from "../shelfStatus";
import { usePlexLibraries } from "../usePlexLibraries";
import { useSafety } from "../useSafety";
import { jobResultText } from "./JobStatus";
import { MANUAL_CONNECTION, ServerPickList, connectionLabel, usePlexPinPoll } from "./PlexPin";
import { StaleReadSlot, collapseStaleReads } from "./StaleReadNotice";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { SetRow } from "./SetRow";

/** The address the Manual address row would save, or "" when it has no host to send. One
 *  declaration, so the Save that composes it and the dirty check that reports this draft up to
 *  `Settings` can never disagree about what the row is holding. */
function manualUri(host: string, port: string, ssl: boolean): string {
  const trimmed = host.trim();
  if (!trimmed) return "";
  return `${ssl ? "https" : "http"}://${trimmed}:${port.trim() || "32400"}`;
}

/** Parses a stored address into the three manual fields, the same way for both readers:
 *  `openManual` fills the row from it, and the dirty check below compares against it to see
 *  what "nothing typed" looks like.
 *
 *  Both sides use this parser because the stored string and the row's own composition of it
 *  are not the same text, so comparing them directly would report edits nobody made.
 *  `URL.hostname` lowercases a host typed with capitals, and `URL.port` is empty for a scheme's
 *  default port, so the port has to be read back from the parsed URL rather than assumed:
 *  assuming 32400 for http would show a server running on port 80 as though it were on 32400. */
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
 *  `held_back === null` means no scan has recorded a value yet, which is different from zero, so
 *  the line says nothing rather than claiming none. Zero is a real answer and is reported
 *  plainly, because "none" is exactly what tells an operator to leave this control alone.
 *
 *  This line reports what was counted, never what was decided. The stored number is items whose
 *  plays stopped being readable; nothing here consults the deletion verdict. Calling them "held
 *  back" or "kept" would claim a protection this figure does not track: three separate gates
 *  decide whether an item is actually protected, and an operator can turn all three off while
 *  this count keeps rising.
 *
 *  The line avoids the words "held back" for a second reason: the app already uses that phrase
 *  for an item with no readable size, on the planner and in several docs pages, where the fix is
 *  a policy allowance. Reusing it here would send the operator to fix the wrong thing.
 *
 *  It names the LAST SCAN rather than "right now" because that is where the number comes from:
 *  it does not move until the next scan runs, which is also why resetting the record leaves
 *  this line unchanged until then. */
function watchEvidenceStatus(evidence: WatchEvidence): string {
  if (evidence.held_back === null) {
    return i18next.t("plex.watchEvidence.holding", { n: evidence.titles });
  }
  if (evidence.held_back === 0) {
    return i18next.t("plex.watchEvidence.holdingClean", { n: evidence.titles });
  }
  return i18next.t("plex.watchEvidence.holdingUnreadable", {
    n: evidence.titles,
    items: evidence.held_back,
  });
}

export function PlexPanel({
  /** Called whenever this panel gains or loses an unsaved draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const { t } = useTranslation();
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
  // `forgetPassword` stays out of `hasDrafts` below: retyping a confirm credential costs three
  // seconds, not real work, and `DeletionToggle` treats its own confirm the same way.
  // `onDirtyChange` exists to stop the operator walking away from something they cannot get back.
  const [forgetPassword, setForgetPassword] = useState("");
  // The web-address box mirrors the saved value and follows it when a save, or another tab,
  // changes it. Typing in the box diverges the two until Save or a refetch reconciles them.
  const [webUrl, setWebUrl] = useState("");
  const [webUrlError, setWebUrlError] = useState<string | null>(null);
  // Both inline Saves on this panel exist only while their row is dirty, so pressing one removes
  // the button itself, and each stays disabled while its write is in flight. That leaves focus at
  // `<body>` once the button is gone, unless something claims it first. Neither row has a heading
  // of its own, so each hands focus to the control that outlives it, and the two controls differ:
  // the web address box survives its own save and still holds the value just committed, while the
  // whole Manual address row collapses, leaving the Connection select above it, which is where
  // the operator opened the row from and now reads back the saved address. Two hooks, because the
  // two targets settle at different times: the web address waits for the `["plex"]` refetch, and
  // the manual row collapses inside `onSuccess`, with its select staying disabled until the write
  // clears.
  const afterWebUrlSave = useSuccessorFocus();
  const afterManualSave = useSuccessorFocus();
  const savedWebUrl = data?.web_url ?? "";
  // Tracks which stored value the box was last seeded from. The seeding effect below runs after
  // React commits, so on the render right after `data` first arrives, the box is still empty
  // against a real stored address, and a naive dirty check would report a draft nobody typed.
  // This panel re-seeds every time the stored value changes, not just once on load, since a save
  // or another tab can move the address again, so a plain "have we seeded" flag would not be
  // enough; comparing against the last-seeded value covers both cases.
  //
  // The initial value is `null`, never `savedWebUrl`. `["plex"]` stays cached and `Settings`
  // remounts this panel on every section switch, so on a return visit `data` can already be
  // present on the very first render: `savedWebUrl` would then equal the still-empty box, and
  // the dirty check would report a draft nobody typed. `null` cannot equal any real stored
  // value, so it fails closed on that first render and still re-seeds correctly on every later
  // change.
  const [seededFrom, setSeededFrom] = useState<string | null>(null);
  useEffect(() => {
    setWebUrl(savedWebUrl);
    setSeededFrom(savedWebUrl);
  }, [savedWebUrl]);

  // The certificate check. Before linking, it rides along with the link polls so a self-signed
  // server can be reached at all. Once linked, it edits the stored server row directly. The ref
  // keeps the in-flight poll reading the current choice.
  const [verifyCert, setVerifyCert] = useState(true);
  const verifyRef = useRef(true);
  const savedVerify = data?.verify_tls ?? true;
  // The same sentinel pattern as `seededFrom` above. The bug it guards against is latent here:
  // the status read omits the certificate flag while unlinked, and the schema default is also
  // true, so the stored value and this box's initial value already agree and no warm mount can
  // catch them apart. That is a property of one server route, not of this guard, so the guard
  // still holds without relying on it.
  const [verifySeededFrom, setVerifySeededFrom] = useState<boolean | null>(null);
  useEffect(() => {
    setVerifyCert(savedVerify);
    verifyRef.current = savedVerify;
    setVerifySeededFrom(savedVerify);
  }, [savedVerify]);

  // Three of the five paths that change which server is linked are on this panel. The setup
  // wizard holds the other two. The key list, and why it is that list, live with the helper.
  const invalidateAllPlex = () => invalidateAllPlexQueries(queryClient);

  const saveWebUrl = useMutation({
    mutationFn: () => api.setPlexSettings({ web_url: webUrl.trim() }),
    // Re-seeds from the response instead of waiting for the effect above. Clearing the box is
    // how the help text says to restore the hosted default, and that saves an empty string: the
    // route stores it as "unset" and reports back the same default string it was already
    // returning, so `savedWebUrl` never changes and the effect above never re-fires. Without
    // this, the box would sit empty against a default it already matches: the Save button would
    // never go away, and the section-switch confirm would ask to discard a value that is already
    // saved.
    onSuccess: (status) => {
      setWebUrl(status.web_url);
      setWebUrlError(null);
      // The Save button disappearing is a silent success signal, but the manual address row
      // next to it already says "Connection saved" out loud. This keeps the two rows consistent.
      announce(t("plex.webAddress.saved"));
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e) => setWebUrlError(describeError(e)),
  });

  // Flips the stored certificate check on the linked server. It sends only that field, never
  // the address: the server keeps any field this request omits, so sending a stale cached
  // address here could silently revert an address the operator just changed elsewhere, pointing
  // every "open in Plex" link in the app back at plex.tv.
  const saveVerify = useMutation({
    mutationFn: (next: boolean) => api.setPlexSettings({ verify_tls: next }),
    onSuccess: () => {
      setPlexError(null);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    // The toggle flips optimistically. A failed save has to roll it back, so the switch never
    // claims the certificate check is on while the server still has it off. The switch is
    // disabled while the save is pending, so `!next` is the value from before the flip.
    onError: (e, next: boolean) => {
      setVerifyCert(!next);
      verifyRef.current = !next;
      setPlexError(describeError(e));
    },
  });

  // Ends a link attempt, however it ended. It refreshes the whole set of Plex-derived data, not
  // just the status row: linking is one of the three ways the linked server changes, and without
  // a full refresh the library grid, server picker, and Leaving Soon settings would still answer
  // for a server that is no longer the one on screen. Library names like "Movies" and "TV Shows"
  // repeat across servers, so a stale library list can look correct while pointing at the wrong
  // server's libraries.
  const done = () => {
    setLinking(false);
    invalidateAllPlex();
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  const pin = usePlexPinPoll<PlexLinkPoll>({
    poll: (pinId, machineId) => api.plexLinkPoll(pinId, machineId, verifyRef.current),
    onOk: (poll) => {
      const server = poll.server?.name ?? t("plex.linked.defaultServerName");
      const said = t("plex.linked.said", { server });
      setMessage(said);
      // `message` renders as a plain `.muted` paragraph, not a live region, while the failure
      // paths below use `Notice`'s `role="alert"` and so speak on their own. This announce call
      // gives the success path the same spoken confirmation the failure path gets for free.
      announce(said);
      done();
    },
    // The picker's announcement lives in `usePlexPinPoll` (`chooseServerSaid`), not here. Do not
    // restate that sentence in this file: `PlexPin.test.tsx` checks by name that only one copy
    // exists, so a second copy here would fail that test.
    //
    // A sign-in that never completed is a failure, not status, so it goes to `plexError` and
    // renders as an error rather than in the gray slot "Linked to ..." uses.
    onTimedOut: () => {
      setPlexError(t("plex.signInTimedOut"));
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
      // noopener keeps plex.tv from reaching this page. `auth_url` carries a forwardUrl to
      // /plex-done.html so the opened window still closes itself. `Login.PlexButton` uses the
      // same pair.
      window.open(start.auth_url, "_blank", "noopener");
      pin.begin(start.pin_id);
    } catch (e) {
      setPlexError(describeError(e));
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
    // The same whole-set refresh `done` does, and for the same reason: after an unlink there is
    // no linked server, so every row that described "the linked server" would otherwise describe
    // one Reaper can no longer reach.
    onSuccess: () => {
      setPlexError(null);
      invalidateAllPlex();
      void queryClient.invalidateQueries({ queryKey: ["setup"] });
    },
    onError: (e) => setPlexError(describeError(e)),
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
    // Carries the operator's current certificate-check choice, so a switch to a self-signed
    // server probes with the setting they already chose, not the default.
    mutationFn: (machineId: string) => api.plexSwitchServer(machineId, verifyRef.current),
    onSuccess: (_result, machineId) => {
      // A silent success here would be worse than saying nothing: it would also clear the
      // panel's one message slot (below), so switching which server Reaper deletes from would
      // read as quieter than a no-op. Named from the server list rather than its id, falling
      // back to a plain sentence if the picker's own label cannot be found.
      const picked = resources.data?.servers.find((s) => s.machine_identifier === machineId);
      announce(
        picked
          ? t("plex.serverSwitched.named", { name: picked.name })
          : t("plex.serverSwitched.generic"),
      );
      setMessage(null);
      setPlexError(null);
      invalidateAllPlex();
    },
    onError: (e) => setPlexError(describeError(e)),
  });

  const [manualOpen, setManualOpen] = useState(false);
  const [manualHost, setManualHost] = useState("");
  const [manualPort, setManualPort] = useState("32400");
  const [manualSsl, setManualSsl] = useState(true);
  const [connError, setConnError] = useState<string | null>(null);

  const setConnection = useMutation({
    mutationFn: (uri: string) => api.plexSetConnection(uri),
    onSuccess: () => {
      announce(t("plex.connectionSaved"));
      setConnError(null);
      // A successful connection save fixes reachability, so a previous "couldn't reach" error
      // from a failed switch is now stale. Clear it, or a red notice would linger beside a
      // healthy connection.
      setPlexError(null);
      // Collapsing the editor is the only success signal, and it unmounts the currently focused
      // button.
      setManualOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e) => setConnError(describeError(e)),
  });

  // Only the server plex.tv marks `current` counts as the one Reaper is linked to. Falling back
  // to `servers[0]` would let a partial or filtered plex.tv response present a different server
  // as the managed one: the Connection row would list that server's addresses, and saving one
  // would point Reaper's Leaving Soon writes and Never-Reap reads at a server it was never
  // linked to. So an unresolved server stays unresolved: the row says the list came back
  // without it, and both pickers go quiet until it is back.
  const currentServer = resources.data?.servers.find((s) => s.current);
  const linkedServerMissing = resources.data !== undefined && currentServer === undefined;
  const connections = currentServer?.connections ?? [];
  const savedUri = data?.connection_uri ?? "";
  const savedIsDiscovered = connections.some((c) => c.uri === savedUri);
  // A typed-in address keeps its own option value, so "Manual address…" is always a different
  // choice than whichever address is already selected. If they shared one value, picking it
  // again would fire no change event and the editor could never be reopened.
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

  // The list, and the first-visit sync that fills one that has never been synced. Both come from
  // the shared hook, which the wizard's Plex step and the service editor's library pickers also
  // read, so all three screens treat an empty list the same way.
  const { libraries, sync: syncLibraries } = usePlexLibraries({ enabled: linked });
  const saveLibraries = useMutation({
    mutationFn: api.setPlexLibraries,
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
  });

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
  // gates on it reads it from there. `DeletionToggle` branches on the same flag for the same
  // reason.
  const safety = useSafety();
  // Both exits from the confirm form unmount it and take the focused button with them, so
  // without this, focus would fall to `<body>` and the next Tab would restart at the top of the
  // page. The successor is the button that opened the form, which is back in that slot by the
  // next commit. The two Saves above make the same handoff through the same hook.
  const afterForget = useSuccessorFocus();
  const forgetWatchEvidence = useMutation({
    mutationFn: api.resetWatchEvidence,
    // The typed password is dropped on both exits, success and refusal alike. Holding it would
    // leave the admin password sitting in component state for as long as the panel stays
    // mounted, and refill the box the next time it opens. `RestoreCard` clears its own password
    // box on a refusal for the same reason.
    //
    // A refusal keeps the form on screen, so it hands focus to nobody: the Confirm button the
    // operator pressed is still there, still focused, with the error beside it.
    onError: () => setForgetPassword(""),
    onSuccess: (result) => {
      setForgetting(false);
      setForgetPassword("");
      setForgotten(result.forgotten);
      afterForget.arriving();
      // The status line's own sentence, said out loud: it is the only thing that changes, and it
      // sits a few lines down in an unfocused part of the page. Announced from the settled
      // mutation, never when the request is issued.
      announce(t("plex.watchEvidence.forgotten", { n: result.forgotten }));
      // Refetches rather than patching in the new numbers by hand: the reset moves `titles` to
      // zero, but `held_back` describes the LAST SCAN and does not change until the next one
      // runs. Writing both fields here would have to restate that rule and could drift from it.
      void queryClient.invalidateQueries({ queryKey: ["watch-evidence"] });
    },
  });

  // --- Leaving Soon --------------------------------------------------------------

  const leavingSoon = useQuery({
    queryKey: ["leaving-soon-settings"],
    queryFn: api.leavingSoonSettings,
    enabled: linked,
  });
  // The shelf-name box, seeded from the stored name and following it the same way the web
  // address box above does, with the same sentinel and for the same reason. Its Save is inline
  // for the same reason those two are: this panel has no save bar, and this row writes through
  // the Leaving Soon route instead of `saveGeneral`.
  const [shelfName, setShelfName] = useState("");
  const [shelfNameSeededFrom, setShelfNameSeededFrom] = useState<string | null>(null);
  const afterShelfNameSave = useSuccessorFocus();
  const savedShelfName = leavingSoon.data?.name ?? "";
  useEffect(() => {
    setShelfName(savedShelfName);
    setShelfNameSeededFrom(savedShelfName);
  }, [savedShelfName]);

  const saveLeavingSoon = useMutation({
    mutationFn: api.setLeavingSoonSettings,
    onSuccess: (s, sent) => {
      queryClient.setQueryData(["leaving-soon-settings"], s);
      // Re-seeds from the response instead of waiting for the effect above. Clearing the box is
      // how the help text says to restore the default, and that saves an empty string: the route
      // stores it as unset and reports back the same default name it was already returning, so
      // `savedShelfName` never changes and the effect above never re-fires. Without this, the box
      // would sit empty against a name it already matches: the Save button would never go away,
      // and the section-switch confirm would ask to discard a value that is already saved.
      if (sent.name !== undefined) {
        setShelfName(s.name);
        setShelfNameSeededFrom(s.name);
        // The switches beside this row flip visibly on their own. This row's only success
        // signal is the Save button disappearing, which is silent, so this announces it out
        // loud, the same way the web address row above does.
        announce(t("plex.leavingSoon.nameSaved"));
      }
    },
  });

  const lsStatus = (() => {
    if (!leavingSoon.data) return null;
    // Nothing to say while the shelf is off. With the shelf on, the switch that turns it off
    // sits two rows up and already says so, so this line renders nothing here rather than
    // restating it. The Jobs row draws "Off." instead, because that switch lives on a different
    // screen.
    if (!leavingSoon.data.enabled) return null;
    const last = leavingSoon.data.last;
    // A scan that skips the shelf writes no pass, so reading `last` alone would report a shelf
    // that stopped updating as a current verdict, on the one screen where that matters most. The
    // reason for the skip stays on the Jobs row, which is written for it; this line only says
    // that a skip happened and points at where the reason lives.
    const skipped = shelfSkipIsCurrent(leavingSoon.data);
    // A saved rename that no pass has carried across yet. Shown first, because it is the one
    // fact here that contradicts what the operator is looking at: the box two rows up already
    // shows the new name, but their library still shows the old one, and the counts that follow
    // describe the shelf under that old name.
    // Joined with a plain space, the same as the skipped clause below: each half is a complete
    // sentence with its own punctuation, and neither is edited after it is put together.
    const renaming = shelfRenamePending(leavingSoon.data)
      ? t("plex.leavingSoon.status.renaming", { was: leavingSoon.data.applied_name })
      : "";
    const lead = (rest: string) => (renaming ? `${renaming} ${rest}` : rest);
    if (!last) {
      return lead(
        skipped
          ? t("plex.leavingSoon.status.neverRanSkipped")
          : t("plex.leavingSoon.status.neverRan"),
      );
    }
    // Not `count`: `resultText` below is the service's own sentence and already carries
    // comma-grouped numbers, so a browser-locale count beside it would put two different
    // thousands separators in one line. `countBesideServerText` avoids that; its docstring
    // explains why.
    const movies = t("plex.leavingSoon.status.moviesCount", {
      count: countBesideServerText(last.movies),
      n: last.movies,
    });
    const seasons = t("plex.leavingSoon.status.seasonsCount", {
      count: countBesideServerText(last.seasons),
      n: last.seasons,
    });
    // How the pass went is the pass's own reason, composed under `jobs.result.*`, never worded
    // here. The period between the outcome and the line belongs to the `passLine` catalog
    // entry, never appended here: a translated sentence must not be edited after it is
    // composed. A row stored before this field existed composes to "" and falls back to the
    // bare line, so the catalog entry never wraps an empty outcome.
    const resultText = jobResultText(last.result_reason);
    // The counts survive a skip. Nothing was written, so the shelves still hold them, and past
    // tense is the whole correction needed, the same way the Jobs row's counts line handles it.
    const held = skipped
      ? t("plex.leavingSoon.status.heldSkipped")
      : t("plex.leavingSoon.status.held");
    const line = t("plex.leavingSoon.status.line", {
      since: since(last.at),
      movies,
      seasons,
      held,
    });
    if (skipped) return lead(`${t("plex.leavingSoon.status.laterScanSkipped")} ${line}`);
    return lead(
      resultText ? t("plex.leavingSoon.status.passLine", { result: resultText, line }) : line,
    );
  })();

  // What this panel would lose, reported up to `Settings` so leaving the section can stop and ask
  // first. Declared above the early returns below because the hook that reports it must run on
  // every render, including the loading and error states those returns produce.
  //
  // Each draft is tested against the exact condition that renders the row holding it, because
  // the report makes two claims at once: there is something to lose, and the operator can still
  // reach it. A guard that keeps the first claim after the second has gone false becomes a trap:
  // it asks for a discard on a box that is no longer on screen, with the destructive button as
  // the only way out.
  const webUrlDirty = seededFrom === savedWebUrl && webUrl.trim() !== savedWebUrl;
  // This exact value renders the row's Save button below, so the report and the button always
  // agree: they read one shared value instead of two separate copies of the same comparison
  // that could drift apart.
  //
  // The manual row is behind `linked && manualOpen`, so both conditions belong in this claim:
  // unlinking leaves `manualOpen` set while the row itself is gone. An empty host is not a draft
  // to lose, since Save is disabled without one, and neither is an address that already matches
  // the stored one. Both sides of that last comparison go through `seedManual` and `manualUri`,
  // so opening the row without typing anything is non-dirty by construction.
  const manualDraft = manualUri(manualHost, manualPort, manualSsl);
  const savedSeed = seedManual(savedUri);
  const savedManual = manualUri(savedSeed.host, savedSeed.port, savedSeed.ssl);
  const manualDirty = linked && manualOpen && manualDraft !== "" && manualDraft !== savedManual;
  // The certificate switch writes immediately only once a server is linked, through its
  // onChange below (`if (linked) saveVerify.mutate(next)`). Before that, the choice lives only
  // in this component's state and rides along with the link poll through `verifyRef`, so
  // leaving the section here would silently drop it, and the next sign-in would probe a
  // self-signed server with checking back on and fail with nothing on screen to explain it. Its
  // row renders unconditionally, so the reachability half of the claim holds wherever this form
  // does. Once linked, the switch is not a draft at all: it is already saved.
  const certDirty = !linked && verifySeededFrom === savedVerify && verifyCert !== savedVerify;
  // Its row renders behind `linked && leavingSoon.data`, so both conditions belong in the
  // claim: the row is the only way to save this box, and reporting a draft the operator cannot
  // reach would leave them only the discard button.
  const shelfNameDirty =
    linked &&
    !!leavingSoon.data &&
    shelfNameSeededFrom === savedShelfName &&
    shelfName.trim() !== savedShelfName;
  const hasDrafts = webUrlDirty || manualDirty || certDirty || shelfNameDirty;
  useEffect(() => {
    onDirtyChange?.(hasDrafts);
  }, [hasDrafts, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  // Until the status has actually loaded, nothing here may claim a state: an unread query looks
  // exactly like "not linked", which would invite a needless re-link through the whole Plex
  // sign-in over a momentary hiccup. Nothing is dirty here either, since `data` is undefined and
  // the web-address box and its saved value are both "".
  if (plex.isPending) {
    return (
      <div className="panel">
        <h2>{t("common.brand.plex")}</h2>
        <p className="muted">{t("common.loading")}</p>
      </div>
    );
  }
  // Only when there is nothing to render at all, the same test the General panel uses.
  // Branching on `plex.isError` too would trade the whole form for this paragraph the moment a
  // refetch failed after a good load, even though React Query still holds the last good row: the
  // web-address box and its Save would disappear while the typed value stayed in component
  // state, still reported as unsaved to `Settings`, which would then ask to discard an edit the
  // operator could no longer see or undo. Saving the certificate switch alone is enough to
  // trigger a refetch of this query, so a failed refetch has to keep the form on the last good
  // values.
  if (!data) {
    return (
      <div className="panel">
        <h2>{t("common.brand.plex")}</h2>
        <Notice tone="error">{t("common.loadError")}</Notice>
      </div>
    );
  }

  // All four of these are refetched by one `invalidateAllPlex`, so they reach the stale state
  // together. Only a failed REFETCH is listed here: each group below shows its own never-loaded
  // notice when nothing ever landed, which is a different claim and must not collapse the whole
  // panel. `plex` needs no `data` test, since the guard above already returned when it has none.
  const stale = collapseStaleReads(t("plex.stale.panel"), [
    { what: t("plex.stale.connection"), stale: plex.isError },
    { what: t("plex.stale.libraries"), stale: libraries.isError && !!libraries.data },
    {
      what: t("plex.stale.watchHistory"),
      stale: watchEvidence.isError && !!watchEvidence.data,
    },
    {
      what: t("plex.stale.leavingSoon"),
      stale: leavingSoon.isError && !!leavingSoon.data,
    },
  ]);

  return (
    <div className="panel">
      <h2>{t("common.brand.plex")}</h2>
      <p className="blurb">{t("plex.blurb")}</p>

      {/* A failed refetch no longer collapses the whole form: keeping the form is right, but
          keeping it silent is not, since everything below would read as current when it is
          known to be stale.

          This slot also carries the collapsed line, because it is the only one that sits above
          all four groups below: `invalidateAllPlex` refetches every read on this panel, so one
          failed switch against an unreachable Plex would otherwise draw the same notice four
          times down the page. */}
      <StaleReadSlot plan={stale} slot={t("plex.stale.connection")} />

      <div className="set-group">
        <h3>{t("plex.connectionGroup.heading")}</h3>
        <div className="set-rows">
          {linked && data ? (
            /* One Unlink button, not a box, so it releases the control track
               (`.set-row-plain`). */
            <SetRow
              variant="plain"
              /* Lead with the person signed in, not the server name, which lives one row down in
                 the Server picker. The account name comes from plex.tv, which always resolves
                 after the fast, local-only status query above, so this shows a neutral
                 placeholder while it is in flight rather than flashing the server name. It falls
                 back to the server name only once resources has actually settled without
                 finding a username, meaning plex.tv could not be reached. */
              label={
                resources.isPending
                  ? t("common.loading")
                  : (resources.data?.owner_username ?? data.name)
              }
              help={t("plex.linkedAccount.help", { uri: data.connection_uri })}
            >
              <button className="ghost" onClick={() => unlink.mutate()} disabled={unlink.isPending}>
                {t("plex.unlinkButton")}
              </button>
            </SetRow>
          ) : pin.servers ? (
            <SetRow
              controlClass="server-pick"
              label={t("plex.chooseServer.label")}
              help={t("plex.chooseServer.help")}
            >
              <ServerPickList
                servers={pin.servers}
                onPick={(machineId) => void pin.pick(machineId)}
                onCancel={cancelChoice}
              />
            </SetRow>
          ) : (
            <SetRow label={t("plex.noServer.label")} help={t("plex.noServer.help")}>
              {linking ? (
                // The same wait the login screen shows, worded the same: a fallback link
                // for a blocked popup, and a way out that stops the polling.
                <div className="plex-waiting">
                  <span className="spinner" aria-hidden="true" />
                  <div>
                    <strong>{t("plex.waitingForPlex")}</strong>
                    {/* Once the sign-in is approved, the wait can continue for a second
                          reason: the server itself isn't answering yet. Say which one it is,
                          so a longer wait doesn't read as a hang. Reaper keeps polling either
                          way, and the sign-in stays good. */}
                    <p className="muted">
                      {pin.retrying ??
                        (authUrl !== "" ? (
                          <Trans
                            i18nKey="plex.waiting.approveWithLink"
                            components={{
                              btn: <a href={authUrl} target="_blank" rel="noreferrer" />,
                            }}
                          />
                        ) : (
                          <Trans i18nKey="plex.waiting.approveNoLink" />
                        ))}
                    </p>
                  </div>
                  <button className="link" onClick={cancelLink}>
                    {t("common.cancel")}
                  </button>
                </div>
              ) : (
                <button className="btn-plex" onClick={startLink}>
                  {t("plex.linkButton")}
                </button>
              )}
            </SetRow>
          )}

          {linked && (
            <SetRow
              label={t("plex.serverField.label")}
              help={
                <>
                  {t("plex.server.help")}
                  {resources.data?.source === "stored" && ` ${t("plex.server.helpStoredNote")}`}
                </>
              }
            >
              {resources.isPending ? (
                <span className="muted">{t("plex.server.looking")}</span>
              ) : resources.isError ? (
                <>
                  <span className="muted">{t("plex.server.listError")}</span>
                  <button className="ghost sm" onClick={() => void resources.refetch()}>
                    {t("plex.server.retry")}
                  </button>
                </>
              ) : (
                <>
                  {/* `standing`, because a plex.tv response missing the linked server makes
                        this true on the panel's first successful read, so it describes what
                        this row looks like until the server is back rather than a reply to a
                        specific action. The action failures further down answer their own
                        button presses and stay ordinary alerts. */}
                  {linkedServerMissing && (
                    <Notice tone="warn" standing>
                      {data?.name
                        ? t("plex.server.missingNamed", { name: data.name })
                        : t("plex.server.missingUnnamed")}
                    </Notice>
                  )}
                  <select
                    value={currentServer?.machine_identifier ?? ""}
                    aria-label={t("plex.serverField.label")}
                    disabled={switchServer.isPending || linkedServerMissing}
                    onChange={(e) => {
                      const next = e.target.value;
                      if (next && next !== currentServer?.machine_identifier) {
                        switchServer.mutate(next);
                      }
                    }}
                  >
                    {linkedServerMissing ? (
                      // A select whose value matches no option displays its first option, so
                      // listing every server here would still show one of them as the current
                      // server, which is the misreading this branch avoids. The box instead
                      // names what Reaper actually uses.
                      <option value="">{data?.name ?? t("plex.server.fallbackOptionLabel")}</option>
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
                    title={t("plex.server.refreshTitle")}
                  >
                    {resources.isFetching ? t("common.refreshing") : t("common.refresh")}
                  </button>
                </>
              )}
            </SetRow>
          )}

          {linked && (
            <SetRow label={t("plex.connectionField.label")} help={t("plex.connectionField.help")}>
              <select
                ref={afterManualSave.ref as RefObject<HTMLSelectElement>}
                value={connectionValue}
                aria-label={t("plex.connectionField.label")}
                // Without the linked server there is nothing to list but the saved address: any
                // other choice here would point at a different server's addresses.
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
                  <option value={savedUri}>
                    {t("plex.connection.manualOption", { uri: savedUri })}
                  </option>
                )}
                <option value={MANUAL_CONNECTION}>
                  {t("plex.connection.manualAddressOption")}
                </option>
              </select>
            </SetRow>
          )}

          {linked && manualOpen && (
            // A cluster, not a box: host, port, an SSL switch and Save belong on one line, so
            // this row keeps the shrink-to-fit control column (see `.set-row-cluster`).
            <SetRow
              variant="cluster"
              label={t("plex.manualAddress.label")}
              help={t("plex.manualAddress.help")}
            >
              {/* Two boxes under one `.set-label`, named the way the accent row names its own
                    pair: the row's label, then which half this is. A placeholder is a name of
                    last resort, so without these labels the host box would announce itself
                    using the example address inside it. */}
              <input
                type="text"
                className="input-host"
                value={manualHost}
                aria-label={t("plex.manualAddress.hostAriaLabel")}
                onChange={(e) => setManualHost(e.target.value)}
                placeholder={t("plex.manualAddress.hostPlaceholder")}
                autoComplete="off"
              />
              <input
                type="text"
                className="input-port"
                value={manualPort}
                aria-label={t("plex.manualAddress.portAriaLabel")}
                onChange={(e) => setManualPort(e.target.value.replace(/\D/g, ""))}
                placeholder="32400"
                inputMode="numeric"
              />
              <label className="toggle" title={t("plex.manualAddress.useSslLabel")}>
                <Switch
                  checked={manualSsl}
                  onChange={setManualSsl}
                  ariaLabel={t("plex.manualAddress.useSslLabel")}
                />
                <span>{t("plex.manualAddress.sslShort")}</span>
              </label>
              <button
                className="primary sm"
                disabled={!manualHost.trim() || setConnection.isPending}
                onClick={() => {
                  afterManualSave.arriving();
                  saveManual();
                }}
              >
                {setConnection.isPending ? t("common.checking") : t("common.save")}
              </button>
            </SetRow>
          )}

          {/* A Switch, not a box, so it releases the control track (`.set-row-plain`). */}
          <SetRow
            variant="plain"
            label={t("services.modal.field.verifyCert")}
            help={t("plex.verifyCert.help")}
            after={!verifyCert && <Notice tone="warn">{t("plex.verifyCert.warning")}</Notice>}
          >
            <Switch
              checked={verifyCert}
              disabled={saveVerify.isPending}
              ariaLabel={t("services.modal.field.verifyCert")}
              onChange={(next) => {
                setVerifyCert(next);
                verifyRef.current = next;
                if (linked) saveVerify.mutate(next);
              }}
            />
          </SetRow>

          {/* This row and Manual address above keep their own inline Save, unlike the General
                panel's single save bar for the whole panel. That is deliberate: these two save
                through different routes than `saveGeneral`, so combining them would need a
                shared draft model this panel does not otherwise need. The layout does account
                for it: the box grows into its track and gives width back from the right, so its
                left edge and its text hold still as this button appears. The section-switch
                confirm also covers these two rows, through `onDirtyChange` above: a panel does
                not need a save bar to be asked about before it unmounts. A future save bar for
                Plex should take both rows together. */}
          <SetRow label={t("plex.webAddress.label")} help={t("plex.webAddress.help")}>
            <input
              type="url"
              ref={afterWebUrlSave.ref as RefObject<HTMLInputElement>}
              value={webUrl}
              aria-label={t("plex.webAddress.label")}
              onChange={(e) => {
                setWebUrl(e.target.value);
                setWebUrlError(null);
              }}
              placeholder={t("plex.webAddress.placeholder")}
              autoComplete="off"
            />
            {/* Reads `webUrlDirty` above rather than repeating its comparison, so the dirty
                  report and this button always agree. */}
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
                {saveWebUrl.isPending ? t("common.saving") : t("common.save")}
              </button>
            )}
          </SetRow>
        </div>

        {connError && <Notice tone="error">{connError}</Notice>}
        {webUrlError && <Notice tone="error">{webUrlError}</Notice>}
        {plexError && <Notice tone="error">{plexError}</Notice>}
        {message && <p className="muted">{message}</p>}
      </div>

      {linked && (
        <div className="set-group">
          <h3>{t("plex.librariesGroup.heading")}</h3>
          <p className="group-blurb">{t("plex.librariesGroup.blurb")}</p>
          {/* Only when there is nothing to render at all, the same divided test the panel's own
              status read uses above. Branching on `isError` alone would trade the whole grid,
              every per-library Switch, and the Refresh button for one paragraph even while React
              Query still holds the last good list.

              `invalidateAllPlex` refetches this key, and all three paths that change which
              server is linked call it: `switchServer.onSuccess`, `done` (the link path), and
              `unlink.onSuccess`. `SetupWizard` also refetches it by firing a bare
              `invalidateQueries()` when the first scan ends and then rendering this panel on
              the same screen.

              Which failure arm a link lands on depends on whether this key has ever been read.
              On a fresh install it has not: the query is `enabled: linked`, so linking mounts it
              for the first time, and a failure there is the never-loaded arm above. After an
              unlink it has: invalidating marks the entry stale but keeps it mounted, so linking
              a different server refetches with the PREVIOUS server's list still in hand, and a
              failure lands on the stale arm instead, with the notice showing. There is also a
              brief window, between the link landing and the refetch resolving, where neither
              `isPending` nor `isError` is set, so the list renders unmarked for a moment. Both
              arms are bounded: `set_plex_libraries` walks the server's own stored list and
              ignores keys it does not contain, so a press during that window cannot mis-target a
              library.

              The list is a read, not a draft, so what is lost here is smaller than the General
              panel's typed value, but a switch the operator cannot see is still a library they
              cannot turn off. */}
          {libraries.isPending || syncLibraries.isPending ? (
            <p className="muted">{t("plex.libraries.loading")}</p>
          ) : libraries.isError && !libraries.data ? (
            <Notice tone="error">{t("plex.libraries.loadError")}</Notice>
          ) : (
            <>
              <StaleReadSlot plan={stale} slot={t("plex.stale.libraries")} />
              <div className="lib-head">
                <span className="muted">
                  {t("plex.libraries.foundCount", { n: (libraries.data ?? []).length })}
                </span>
                <button
                  className="ghost sm"
                  disabled={syncLibraries.isPending}
                  onClick={() => syncLibraries.mutate()}
                >
                  {t("plex.libraries.refreshButton")}
                </button>
              </div>
              <div className="lib-grid">
                {(libraries.data ?? []).map((lib) => (
                  <div key={lib.key} className={lib.enabled ? "lib-card" : "lib-card off"}>
                    <span>
                      {lib.title}
                      <span className="lib-kind">
                        {lib.kind === "movie"
                          ? t("plex.libraries.kindMovie")
                          : t("plex.libraries.kindTv")}
                      </span>
                    </span>
                    <Switch
                      checked={lib.enabled}
                      disabled={saveLibraries.isPending}
                      ariaLabel={t("plex.libraries.toggleLabel", { title: lib.title })}
                      onChange={(next) => toggleLibrary(lib.key, next)}
                    />
                  </div>
                ))}
              </div>
              {(saveLibraries.error || syncLibraries.error) && (
                <Notice tone="error">
                  {describeError(saveLibraries.error ?? syncLibraries.error)}
                </Notice>
              )}
            </>
          )}
        </div>
      )}

      {linked && (
        <div className="set-group">
          <h3>{t("plex.watchHistoryGroup.heading")}</h3>
          <p className="group-blurb">{t("plex.watchHistoryGroup.blurb")}</p>
          {/* Same shape as the two groups around it: a failed read keeps the last good answer on
              screen and says so, rather than blanking a control the operator came here for. */}
          {watchEvidence.isPending ? (
            <p className="muted">{t("common.loading")}</p>
          ) : !watchEvidence.data ? (
            <Notice tone="error">{t("plex.watchHistory.loadError")}</Notice>
          ) : (
            <>
              <StaleReadSlot plan={stale} slot={t("plex.stale.watchHistory")} />
              <div className="set-rows">
                <SetRow
                  variant="plain"
                  label={t("plex.watchHistory.label")}
                  help={t("plex.watchHistory.help")}
                >
                  {/* Five explicit states. The three that are not the form all fail closed:
                      this control withdraws a protection, so a safety read Reaper could not
                      complete offers nothing to press, rather than assuming a password exists
                      to check against. `DeletionToggle` splits the same way and keeps its OFF
                      direction live on an unreadable state, because that direction can only
                      make Reaper safer. This control has no such safe direction, which is why
                      an unreadable safety state ends the branch here instead. */}
                  {safety.isLoading ? (
                    <span className="muted">{t("common.checking")}</span>
                  ) : !safety.data ? (
                    <span className="muted">{t("plex.watchHistory.safetyUnknown")}</span>
                  ) : !safety.data.has_password ? (
                    <span className="muted">{t("common.noAdminPassword")}</span>
                  ) : forgetting ? (
                    /* The same form as arming deletion: one password box, Confirm, Cancel. The
                         placeholder is a hint that disappears on the first keystroke, so the
                         field is still named by its label either way. `pw-inline` applies
                         because this form sits in a settings row, not the Security panel's
                         field pane: `.pw-form` alone lays out as a column, which would stretch
                         the box and both buttons to full width on a phone. */
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
                        placeholder={t("common.adminPasswordPlaceholder")}
                        aria-label={t("common.adminPassword")}
                        autoComplete="current-password"
                        autoFocus
                      />
                      {/* `danger` and a plain button, the same pair this row had before the
                            password box joined them. Not `sm`, which the arming form uses even
                            though no stylesheet defines it there: an inert class name still
                            reads as a deliberate size choice, so it is left alone rather than
                            removed, since removing a no-op class moves no pixel. */}
                      <button
                        type="submit"
                        className="danger"
                        disabled={!forgetPassword || forgetWatchEvidence.isPending}
                      >
                        {t("plex.watchHistory.confirmButton")}
                      </button>
                      {/* Cancel drops the typed password along with the form, the same as the
                            arming form's own Cancel. */}
                      <button
                        type="button"
                        disabled={forgetWatchEvidence.isPending}
                        onClick={() => {
                          afterForget.arriving();
                          setForgetting(false);
                          setForgetPassword("");
                        }}
                      >
                        {t("common.cancel")}
                      </button>
                    </form>
                  ) : (
                    <button
                      ref={afterForget.ref as RefObject<HTMLButtonElement>}
                      className="ghost"
                      title={t("plex.watchHistory.forgetTitle")}
                      onClick={() => {
                        setForgotten(null);
                        setForgetting(true);
                      }}
                    >
                      {t("plex.watchHistory.forgetButton")}
                    </button>
                  )}
                </SetRow>
                <div className="set-row set-status">
                  {/* Says when the reset takes effect, because nothing on any surface moves
                      until the next scan: the record is gone, but the stored candidates and
                      their facts are frozen snapshot data. Without that second sentence, an
                      operator reading the unchanged queue could conclude the reset did not
                      work and reach for the policy instead, which does cost files. The restore
                      notice and the shelf hint use the same wording for the same reason. */}
                  {forgotten !== null
                    ? t("plex.watchEvidence.forgotten", { n: forgotten })
                    : watchEvidenceStatus(watchEvidence.data)}
                </div>
              </div>
              {/* The server's own sentence, not a fixed one. Every refusal here needs a
                  different response from the operator: a password that did not match, one
                  never set, too many tries, and one fixed sentence covering all of them would
                  name none of them clearly. Each server sentence already states plainly that
                  the record was kept, so no lead-in is added here either, or the reassurance
                  would print twice. `DeletionToggle` renders its own arming failures the same
                  way. */}
              {forgetWatchEvidence.error && (
                <Notice tone="error">
                  {describeError(forgetWatchEvidence.error) ||
                    t("plex.watchEvidence.forgetFailedFallback")}
                </Notice>
              )}
              {/* `standing`: it is always on screen, so it must not announce itself as an
                  alert every time this panel renders. */}
              <Notice tone="warn" standing>
                {t("plex.watchHistory.tautulliWarning")}
              </Notice>
              <p className="group-hint muted">{t("plex.watchHistory.tautulliHint")}</p>
            </>
          )}
        </div>
      )}

      {linked && (
        <div className="set-group">
          <h3>{t("plex.leavingSoonGroup.heading")}</h3>
          <p className="group-blurb">{t("plex.leavingSoonGroup.blurb")}</p>
          {/* Checks `!leavingSoon.data` alone. A read that never landed leaves it undefined and
              still reaches the sentence below, so dropping an `isError ||` in front of it costs
              nothing there, while it also stops a failed refetch from taking both switches off
              screen while the last good answer is still held. These two switches control the
              shelf's on/off state and its read-only override, which decides whether Reaper
              writes to Plex before deletion is armed, so blanking them would hide a control the
              operator came here to set. */}
          {leavingSoon.isPending ? (
            <p className="muted">{t("common.loading")}</p>
          ) : !leavingSoon.data ? (
            <Notice tone="error">{t("plex.leavingSoon.loadError")}</Notice>
          ) : (
            <>
              <StaleReadSlot plan={stale} slot={t("plex.stale.leavingSoon")} />
              <div className="set-rows">
                {/* The name the two rows below refer to, so it comes first. Its own inline Save,
                  like the web address row above and for the same reason. `maxLength` matches
                  the route's own bound (`app_settings.LEAVING_SOON_NAME_MAX`), so a name that
                  is too long is stopped in the box instead of coming back as a 422. */}
                <SetRow
                  label={t("plex.leavingSoon.nameLabel")}
                  help={t("plex.leavingSoon.nameHelp")}
                >
                  <input
                    type="text"
                    ref={afterShelfNameSave.ref as RefObject<HTMLInputElement>}
                    value={shelfName}
                    aria-label={t("plex.leavingSoon.nameLabel")}
                    onChange={(e) => setShelfName(e.target.value)}
                    maxLength={60}
                    autoComplete="off"
                  />
                  {shelfNameDirty && (
                    <button
                      type="button"
                      className="primary sm"
                      disabled={saveLeavingSoon.isPending}
                      onClick={() => {
                        afterShelfNameSave.arriving();
                        saveLeavingSoon.mutate({ name: shelfName.trim() });
                      }}
                    >
                      {saveLeavingSoon.isPending ? t("common.saving") : t("common.save")}
                    </button>
                  )}
                </SetRow>
                {/* Both rows here carry a Switch and nothing else, so they use the plain control
                  track (`.set-row-plain`). Each names the shelf, so each reads the STORED name
                  rather than the box above: until Save lands, the name in the box is not what
                  either switch actually affects. */}
                <SetRow
                  variant="plain"
                  label={t("plex.leavingSoon.enableLabel", { shelf: savedShelfName })}
                  help={t("plex.leavingSoon.enableHelp", { shelf: savedShelfName })}
                >
                  <Switch
                    checked={leavingSoon.data.enabled}
                    disabled={saveLeavingSoon.isPending}
                    ariaLabel={t("plex.leavingSoon.enableLabel", { shelf: savedShelfName })}
                    onChange={(enabled) => saveLeavingSoon.mutate({ enabled })}
                  />
                </SetRow>
                <SetRow
                  variant="plain"
                  label={t("plex.leavingSoon.allowUnarmedLabel")}
                  help={t("plex.leavingSoon.allowUnarmedHelp")}
                >
                  <Switch
                    checked={leavingSoon.data.allow_unarmed}
                    disabled={saveLeavingSoon.isPending}
                    ariaLabel={t("plex.leavingSoon.allowUnarmedLabel")}
                    onChange={(allow_unarmed) => saveLeavingSoon.mutate({ allow_unarmed })}
                  />
                </SetRow>
                {lsStatus && (
                  <div className="set-row set-status">
                    <span>{lsStatus}</span>
                  </div>
                )}
              </div>
            </>
          )}
          {saveLeavingSoon.error && (
            <Notice tone="error">{describeError(saveLeavingSoon.error)}</Notice>
          )}
        </div>
      )}

      {!linked && <p className="help">{t("plex.linkPrompt")}</p>}
    </div>
  );
}
