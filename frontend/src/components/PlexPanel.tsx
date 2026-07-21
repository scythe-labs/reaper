// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Plex settings panel: link an account, pick which server and address Reaper uses,
// choose the libraries it may touch, and turn the "Leaving Soon" shelf on.
//
// Linking is optional. Scanning reads from Radarr and Sonarr, so everything here is about
// what Reaper may show and write *in Plex*. The sign-in itself is the shared PIN flow in
// PlexPin.tsx, the same one the login screen uses.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type PlexLinkPoll, type PlexResourceConnection } from "../api";
import { count, since } from "../format";
import { ServerPickList, usePlexPinPoll } from "./PlexPin";
import { Switch } from "./Switch";

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

export function PlexPanel() {
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
  // The web-address box mirrors the saved value and follows it when a save (or another
  // tab) changes it; typing diverges the two until Save or a refetch reconciles them.
  const [webUrl, setWebUrl] = useState("");
  const [webUrlError, setWebUrlError] = useState<string | null>(null);
  const savedWebUrl = data?.web_url ?? "";
  useEffect(() => setWebUrl(savedWebUrl), [savedWebUrl]);

  // The certificate check. Before linking it rides along with the link polls (so a
  // self-signed server can be reached at all); once linked it edits the stored server
  // row directly. The ref keeps the in-flight poll reading the current choice.
  const [verifyCert, setVerifyCert] = useState(true);
  const verifyRef = useRef(true);
  const savedVerify = data?.verify_tls ?? true;
  useEffect(() => {
    setVerifyCert(savedVerify);
    verifyRef.current = savedVerify;
  }, [savedVerify]);

  const saveWebUrl = useMutation({
    mutationFn: () => api.setPlexWebUrl(webUrl.trim()),
    onSuccess: () => {
      setWebUrlError(null);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setWebUrlError(e.message),
  });

  // Flip the stored certificate check on the linked server. Sends the SAVED web
  // address, never the box's half-typed one, so this toggle cannot save a URL edit.
  const saveVerify = useMutation({
    mutationFn: (next: boolean) => api.setPlexWebUrl(savedWebUrl, next),
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

  const done = () => {
    setLinking(false);
    void queryClient.invalidateQueries({ queryKey: ["plex"] });
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  const pin = usePlexPinPoll<PlexLinkPoll>({
    poll: (pinId, machineId) => api.plexLinkPoll(pinId, machineId, verifyRef.current),
    onOk: (poll) => {
      setMessage(`Linked to ${poll.server?.name ?? "your server"}.`);
      done();
    },
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
    onSuccess: () => {
      setPlexError(null);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
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

  const invalidateAllPlex = () => {
    void queryClient.invalidateQueries({ queryKey: ["plex"] });
    void queryClient.invalidateQueries({ queryKey: ["plex-resources"] });
    void queryClient.invalidateQueries({ queryKey: ["plex-libraries"] });
    void queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] });
  };

  const switchServer = useMutation({
    // Carry the operator's current certificate-check choice, so switching to a
    // self-signed server they have already turned it off for probes correctly.
    mutationFn: (machineId: string) => api.plexSwitchServer(machineId, verifyRef.current),
    onSuccess: () => {
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
      setConnError(null);
      // A successful connection save fixes reachability, so a prior "couldn't reach"
      // from a failed switch is now stale: clear it, or a red notice lingers beside a
      // healthy connection.
      setPlexError(null);
      setManualOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => setConnError(e.message),
  });

  const currentServer =
    resources.data?.servers.find((s) => s.current) ?? resources.data?.servers[0];
  const connections = currentServer?.connections ?? [];
  const savedUri = data?.connection_uri ?? "";
  const savedIsDiscovered = connections.some((c) => c.uri === savedUri);
  // A typed-in address keeps its own option value, so "Manual address…" is always a
  // different choice than the one already selected. Sharing one value meant picking it
  // fired no change event, and the editor could never be reopened.
  const connectionValue = manualOpen ? MANUAL_CONNECTION : savedUri;

  const openManual = () => {
    // Seed the manual fields from wherever Reaper is pointed right now.
    try {
      const parsed = new URL(savedUri);
      setManualHost(parsed.hostname);
      setManualPort(parsed.port || (parsed.protocol === "https:" ? "443" : "32400"));
      setManualSsl(parsed.protocol === "https:");
    } catch {
      setManualHost("");
      setManualPort("32400");
      setManualSsl(true);
    }
    setConnError(null);
    setManualOpen(true);
  };

  const saveManual = () => {
    const host = manualHost.trim();
    if (!host) return;
    const scheme = manualSsl ? "https" : "http";
    setConnection.mutate(`${scheme}://${host}:${manualPort.trim() || "32400"}`);
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

  // Until the status has actually been read, nothing here may claim a state: an unread
  // query looks exactly like "not linked", and that would invite a needless re-link
  // through the whole Plex sign-in over a momentary hiccup.
  if (plex.isPending) {
    return (
      <div className="panel">
        <h2>Plex</h2>
        <p className="muted">Loading…</p>
      </div>
    );
  }
  if (plex.isError || !data) {
    return (
      <div className="panel">
        <h2>Plex</h2>
        <p className="notice notice-error">Couldn't load these settings. Reload to try again.</p>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2>Plex</h2>
      <p className="blurb">
        Linking Plex lets Reaper warn your library with a "Leaving Soon" shelf and read your
        "Never Reap" collection. It's optional. Scanning works without it.
      </p>

      <div className="set-group">
        <h3>Connection</h3>
        <div className="set-rows">
          {linked && data ? (
            <div className="set-row">
              <span className="set-label">{data.name}</span>
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
                This account owns more than one Plex server. Reaper will only ever scan and
                prune the one you pick.
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
                Sign in with Plex and Reaper discovers your servers. It never asks for a
                token by hand.
              </p>
              <div className="set-control">
                {linking ? (
                  // The same wait the login screen shows, worded the same: a fallback link
                  // for a blocked popup, and a way out that stops the polling.
                  <div className="plex-waiting">
                    <span className="spinner" aria-hidden="true" />
                    <div>
                      <strong>Waiting for Plex…</strong>
                      <p className="muted">
                        Approve the sign-in in the Plex window.{" "}
                        {authUrl !== "" && (
                          <a href={authUrl} target="_blank" rel="noreferrer">
                            Didn’t open?
                          </a>
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
                    <select
                      value={currentServer?.machine_identifier ?? ""}
                      disabled={switchServer.isPending}
                      onChange={(e) => {
                        const next = e.target.value;
                        if (next && next !== currentServer?.machine_identifier) {
                          switchServer.mutate(next);
                        }
                      }}
                    >
                      {(resources.data?.servers ?? []).map((s) => (
                        <option key={s.machine_identifier} value={s.machine_identifier}>
                          {s.name}
                        </option>
                      ))}
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
                How Reaper reaches the server. A local address is usually faster; remote
                works from anywhere. Pick "Manual address" to type your own.
              </p>
              <div className="set-control">
                <select
                  value={connectionValue}
                  disabled={setConnection.isPending || resources.isPending}
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
            <div className="set-row">
              <span className="set-label">Manual address</span>
              <p className="help">Hostname or IP, port, and whether to use SSL.</p>
              <div className="set-control">
                <input
                  type="text"
                  value={manualHost}
                  onChange={(e) => setManualHost(e.target.value)}
                  placeholder="plex.example.net"
                  autoComplete="off"
                />
                <input
                  type="text"
                  className="input-port"
                  value={manualPort}
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
                  onClick={saveManual}
                >
                  {setConnection.isPending ? "Checking…" : "Save"}
                </button>
              </div>
            </div>
          )}

          <div className="set-row">
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
              <p className="notice notice-warn">
                Reaper will accept this server's certificate without checking who issued it.
              </p>
            )}
          </div>

          <div className="set-row">
            <span className="set-label">Plex web address</span>
            <p className="help">
              Where links to your library open. Keep the default unless you host your own
              Plex Web. Clear it and save to go back to the default.
            </p>
            <div className="set-control">
              <input
                type="url"
                value={webUrl}
                onChange={(e) => {
                  setWebUrl(e.target.value);
                  setWebUrlError(null);
                }}
                placeholder="https://app.plex.tv"
                autoComplete="off"
              />
              {webUrl.trim() !== savedWebUrl && (
                <button
                  type="button"
                  className="primary sm"
                  disabled={saveWebUrl.isPending}
                  onClick={() => saveWebUrl.mutate()}
                >
                  {saveWebUrl.isPending ? "Saving…" : "Save"}
                </button>
              )}
            </div>
          </div>
        </div>

        {connError && <p className="notice notice-error">{connError}</p>}
        {webUrlError && <p className="notice notice-error">{webUrlError}</p>}
        {plexError && <p className="notice notice-error">{plexError}</p>}
        {message && <p className="muted">{message}</p>}
      </div>

      {linked && (
        <div className="set-group">
          <h3>Libraries</h3>
          <p className="group-blurb">
            The libraries Reaper may touch in Plex. Leaving Soon shelves are managed only in
            libraries you turn on. This doesn't change what gets scanned: scanning reads from
            Radarr and Sonarr.
          </p>
          {libraries.isPending || syncLibraries.isPending ? (
            <p className="muted">Loading libraries…</p>
          ) : libraries.isError ? (
            <p className="notice notice-error">
              Couldn't load the library list. Reload to try again.
            </p>
          ) : (
            <>
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
                <p className="notice notice-error">
                  {(saveLibraries.error ?? syncLibraries.error)?.message}
                </p>
              )}
            </>
          )}
        </div>
      )}

      {linked && (
        <div className="set-group">
          <h3>Leaving Soon</h3>
          <p className="group-blurb">
            While an item counts down its grace period, Reaper can put it on a "Leaving Soon"
            shelf in Plex, so people get a heads-up before it goes: movies in your movie
            libraries, seasons in your TV libraries.
          </p>
          {leavingSoon.isPending ? (
            <p className="muted">Loading…</p>
          ) : leavingSoon.isError || !leavingSoon.data ? (
            <p className="notice notice-error">
              Couldn't load the Leaving Soon settings. Reload to try again.
            </p>
          ) : (
            <div className="set-rows">
              <div className="set-row">
                <span className="set-label">Show "Leaving Soon" in Plex</span>
                <p className="help">
                  Reaper keeps a Leaving Soon collection in each library you turned on above,
                  and puts the matching label on everything in it. Items appear when they
                  start counting down and drop off when they're spared or removed. Updates
                  after every scan, or from the Jobs page.
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
              <div className="set-row">
                <span className="set-label">Update while read-only</span>
                <p className="help">
                  Until deletion is on, Reaper writes nothing to Plex, including this shelf.
                  Turn this on to let the warning appear while Reaper is still read-only. It
                  can only manage the collection and label. It can never remove files.
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
          )}
          {saveLeavingSoon.error && (
            <p className="notice notice-error">{saveLeavingSoon.error.message}</p>
          )}
        </div>
      )}

      {!linked && (
        <p className="help">
          Link Plex to pick libraries and turn on the "Leaving Soon" shelf.
        </p>
      )}
    </div>
  );
}
