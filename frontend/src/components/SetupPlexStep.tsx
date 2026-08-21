// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wizard's second step: connect Plex.
//
// Most operators arrive here already signed in, because on a fresh install the login screen's
// only way in *is* the Plex sign-in and it claims the server. So the step usually opens on the
// linked state and asks only which libraries to use. The sign-in button is here for the owner
// who came in through a local account instead.
//
// **Skipping says what it actually costs.** Without a linked Plex `executor.execute` refuses
// every real run outright -- the check for who is watching runs through Plex -- and
// `api/runs.py` returns that as a 409 before the run even starts. So the honest sentence is
// "scans, but cannot remove", not "loses the Leaving Soon shelf". Plex remains genuinely
// optional for a *scan*: an unlinked Plex adds no degradation, unlike a linked one that fails.
//
// The PIN flow, the owned-server list and their styling are the shared ones (`PlexPin`), not a
// second copy: this screen and Settings put the same question in different words around the
// same list.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type PlexResourceConnection, type SetupStatus } from "../api";
import i18next from "../i18n";
import { invalidateAllPlex } from "../plexServerQueries";
import { usePlexLibraries } from "../usePlexLibraries";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";
import { ServerPickList, usePlexPinPoll } from "./PlexPin";
import { StepCard } from "./SetupStepper";
import { Switch } from "./Switch";

/** What a discovered address is called in the list. The same shape `PlexPanel`'s own
 *  `connectionLabel` builds, so an address reads the same in both places -- and the same
 *  `plex.connectionKind.*` / `plex.connectionLabel.*` catalog keys, reused rather than
 *  re-minted, so the two never drift into different words for the same thing (rule 72). */
function connectionLabel(c: PlexResourceConnection): string {
  const kind = c.relay
    ? i18next.t("plex.connectionKind.relay")
    : c.local
      ? i18next.t("plex.connectionKind.local")
      : i18next.t("plex.connectionKind.remote");
  let host = c.uri.replace(/^https?:\/\//, "");
  const direct = /^(\d+)-(\d+)-(\d+)-(\d+)\.[0-9a-f]+\.plex\.direct(:\d+)?$/i.exec(host);
  if (direct) host = `${direct[1]}.${direct[2]}.${direct[3]}.${direct[4]}${direct[5] ?? ""}`;
  return c.protocol === "https"
    ? i18next.t("plex.connectionLabel.secure", { kind, host })
    : i18next.t("plex.connectionLabel.plain", { kind, host });
}

/** The sentinel the connection select uses for "let me type one". Not a URI, so it can never
 *  collide with a discovered address. */
const MANUAL = "__manual__";

export function SetupPlexStep({ setup, onNext }: { setup: SetupStatus; onNext: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const linked = setup.plex_linked;
  const [error, setError] = useState<string | null>(null);
  const [linking, setLinking] = useState(false);

  // Linking here changes which server is linked exactly as the settings panel's link does, so
  // it drops the same set. It used to name three of the six keys, which cost nothing only
  // because the wizard renders in place of the app: the consumers of the missing three are
  // unmounted while it is up. That is a reason it never showed, not a reason to keep a second
  // copy of the list -- `setup` is this component's own and is not part of the set.
  const refreshPlex = async () => {
    await queryClient.invalidateQueries({ queryKey: ["setup"] });
    invalidateAllPlex(queryClient);
  };

  const pin = usePlexPinPoll({
    poll: (pinId, machineId) => api.plexLinkPoll(pinId, machineId),
    onOk: () => {
      setLinking(false);
      announce(t("setup.plex.announceConnected"));
      void refreshPlex();
    },
    onTimedOut: () => {
      setLinking(false);
      setError(t("setup.plex.signInTimeoutError"));
    },
    onFailed: (message) => {
      setLinking(false);
      setError(message);
    },
  });

  const startLink = async () => {
    setError(null);
    try {
      const start = await api.plexLinkStart();
      // noopener keeps plex.tv from reaching this page, and `auth_url` carries a forwardUrl
      // to /plex-done.html so the window still closes itself. Same pair as Login.PlexButton.
      window.open(start.auth_url, "_blank", "noopener");
      setLinking(true);
      pin.begin(start.pin_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("setup.plex.signInStartError"));
    }
  };

  return (
    <StepCard step="plex" title={t("setup.plex.title")}>
      {linked ? (
        <PlexLinked onError={setError} />
      ) : pin.servers ? (
        <>
          <p className="blurb">{t("plex.chooseServer.help")}</p>
          <div className="server-pick">
            <ServerPickList
              servers={pin.servers}
              onPick={(machineId) => void pin.pick(machineId)}
              onCancel={() => {
                pin.cancel();
                setLinking(false);
              }}
            />
          </div>
        </>
      ) : (
        <>
          <p className="blurb">{t("setup.plex.readOnlyBlurb")}</p>
          {linking ? (
            <div className="plex-waiting">
              <span className="spinner" aria-hidden="true" />
              <div>
                <strong>{t("plex.waitingForPlex")}</strong>
                <p className="muted">{pin.retrying ?? t("plex.waiting.approveNoLink")}</p>
              </div>
              <button
                className="link"
                onClick={() => {
                  pin.cancel();
                  setLinking(false);
                }}
              >
                {t("common.cancel")}
              </button>
            </div>
          ) : (
            <>
              <button className="btn-plex" onClick={() => void startLink()}>
                {t("login.plexButton.signIn")}
              </button>
              <p className="help">{t("setup.plex.signInHelp")}</p>
            </>
          )}
        </>
      )}

      {error && <Notice tone="error">{error}</Notice>}

      {/* No Back. The only step behind this one is the password, which is deliberately
          one-way: its form sends no current password, so on an install that now has one the
          route answers 403 "The current password didn't match" on a screen that never asked
          for it, and each press feeds the per-IP throttle that arming, restore and sign-in all
          read. */}
      <div className="step-actions">
        <span className="spacer" />
        {/* Skipping only means something while Plex is not linked. Once it is, the button
            would offer to abandon work already done. */}
        {!linked && (
          <button className="ghost" onClick={onNext}>
            {t("setup.actions.skipForNow")}
          </button>
        )}
        <button className="primary btn-lg" onClick={onNext}>
          {t("setup.actions.continue")}
        </button>
      </div>
      {!linked && <p className="help">{t("setup.plex.skipHelp")}</p>}
    </StepCard>
  );
}

/** The linked state: who is signed in, which server and address, and which libraries. */
function PlexLinked({ onError }: { onError: (m: string | null) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["plex"], queryFn: api.plexStatus });
  const resources = useQuery({ queryKey: ["plex-resources"], queryFn: api.plexResources });
  // Through the shared hook, so a list that has never been synced fills itself here exactly as
  // it does in Settings. Reading it with a plain query was the whole of #384: this step renders
  // only once Plex is linked, and it showed an empty Libraries grid on every fresh install
  // while telling the service editor's pickers there were no libraries to offer.
  const { libraries, sync: syncLibraries } = usePlexLibraries();

  const [manualOpen, setManualOpen] = useState(false);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("32400");
  const [ssl, setSsl] = useState(false);

  const servers = resources.data?.servers ?? [];
  const current = servers.find((s) => s.current);
  const savedUri = status.data?.connection_uri ?? "";
  const signedInName = current?.name ?? status.data?.name ?? null;

  const switchServer = useMutation({
    mutationFn: (machineId: string) => api.plexSwitchServer(machineId),
    // The panel's switch and this one are the same change, so they drop the same set. `setup`
    // stays untouched: switching servers does not make the install any more or less configured.
    onSuccess: () => {
      onError(null);
      invalidateAllPlex(queryClient);
    },
    onError: (e: Error) => onError(e.message),
  });
  const setConnection = useMutation({
    mutationFn: (uri: string) => api.plexSetConnection(uri),
    onSuccess: async () => {
      onError(null);
      setManualOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["plex"] });
    },
    onError: (e: Error) => onError(e.message),
  });
  const setLibraries = useMutation({
    mutationFn: (keys: number[]) => api.setPlexLibraries(keys),
    onSuccess: (libs) => queryClient.setQueryData(["plex-libraries"], libs),
    onError: (e: Error) => onError(e.message),
  });

  /** The one URI the three manual boxes compose. A blank host saves nothing, and a blank port
   *  falls back to Plex's own 32400 -- the same rule `PlexPanel.manualUri` applies. */
  const manualUri = () => {
    const h = host.trim();
    if (!h) return "";
    return `${ssl ? "https" : "http"}://${h}:${port.trim() || "32400"}`;
  };

  const libs = libraries.data ?? [];
  const toggleLibrary = (key: number, on: boolean) => {
    const next = on
      ? [...libs.filter((l) => l.enabled).map((l) => l.key), key]
      : libs.filter((l) => l.enabled && l.key !== key).map((l) => l.key);
    setLibraries.mutate(next);
  };

  return (
    <>
      <div className="plex-who">
        <span className="tick" aria-hidden="true">
          ✓
        </span>
        <span className="who-name">
          {signedInName
            ? t("setup.plex.signedInTo", { name: signedInName })
            : t("setup.plex.signedIn")}
        </span>
        <span className="who-sub">{savedUri || t("setup.plex.lookingForAddress")}</span>
      </div>

      {/* Two picks, and only one of them is always worth showing. A one-server account never
          sees a select it cannot change; an account with a choice gets it opened. */}
      <details className="advanced" open={servers.length > 1}>
        <summary>{t("setup.plex.serverAndAddressSummary")}</summary>
        <div className="service-form">
          {servers.length > 1 && (
            <label className="field-sm">
              <span className="field-label">{t("plex.serverField.label")}</span>
              <span className="pick-row">
                <select
                  value={current?.machine_identifier ?? ""}
                  aria-label={t("plex.serverField.label")}
                  disabled={switchServer.isPending}
                  onChange={(e) => {
                    if (e.target.value && e.target.value !== current?.machine_identifier) {
                      switchServer.mutate(e.target.value);
                    }
                  }}
                >
                  {servers.map((s) => (
                    <option key={s.machine_identifier} value={s.machine_identifier}>
                      {s.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="ghost"
                  disabled={resources.isFetching}
                  onClick={() => void resources.refetch()}
                >
                  {resources.isFetching ? t("common.refreshing") : t("common.refresh")}
                </button>
              </span>
              <span className="help">{t("plex.server.help")}</span>
            </label>
          )}

          <label className="field-sm">
            <span className="field-label">{t("plex.connectionField.label")}</span>
            <select
              value={manualOpen ? MANUAL : savedUri}
              aria-label={t("plex.connectionField.label")}
              disabled={setConnection.isPending}
              onChange={(e) => {
                if (e.target.value === MANUAL) setManualOpen(true);
                else {
                  setManualOpen(false);
                  if (e.target.value !== savedUri) setConnection.mutate(e.target.value);
                }
              }}
            >
              {(current?.connections ?? []).map((c) => (
                <option key={c.uri} value={c.uri}>
                  {connectionLabel(c)}
                </option>
              ))}
              {savedUri && !(current?.connections ?? []).some((c) => c.uri === savedUri) && (
                <option value={savedUri}>
                  {t("plex.connection.manualOption", { uri: savedUri.replace(/^https?:\/\//, "") })}
                </option>
              )}
              <option value={MANUAL}>{t("plex.connection.manualAddressOption")}</option>
            </select>
            <span className="help">{t("setup.plex.connectionHelp")}</span>
          </label>

          {manualOpen && (
            <div className="field-sm">
              <span className="field-label">{t("plex.manualAddress.label")}</span>
              <div className="manual-row">
                <span className="m-host">
                  <input
                    type="text"
                    value={host}
                    placeholder={t("setup.plex.manualAddressPlaceholder")}
                    aria-label={t("setup.plex.manualAddressAriaLabel")}
                    onChange={(e) => setHost(e.target.value)}
                  />
                </span>
                <span className="m-port">
                  <input
                    type="text"
                    value={port}
                    inputMode="numeric"
                    aria-label={t("setup.plex.manualPortAriaLabel")}
                    onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, "").slice(0, 5))}
                  />
                </span>
                <label className="toggle">
                  <Switch
                    checked={ssl}
                    onChange={setSsl}
                    ariaLabel={t("plex.manualAddress.useSslLabel")}
                  />
                  <span>{t("plex.manualAddress.sslShort")}</span>
                </label>
                <button
                  type="button"
                  className="primary"
                  disabled={manualUri() === "" || setConnection.isPending}
                  onClick={() => setConnection.mutate(manualUri())}
                >
                  {setConnection.isPending ? t("common.saving") : t("common.save")}
                </button>
              </div>
              {/* What would actually be stored, so it is never a guess. */}
              <span className="help manual-uri">
                {manualUri() || t("setup.plex.manualUriPlaceholderHelp")}
              </span>
            </div>
          )}
        </div>
      </details>

      <h3 style={{ marginTop: "1.1rem" }}>{t("plex.librariesGroup.heading")}</h3>
      <p className="blurb">{t("plex.librariesGroup.blurb")}</p>
      {libraries.isPending || syncLibraries.isPending ? (
        <p className="muted">{t("plex.libraries.loading")}</p>
      ) : libraries.isError && !libraries.data ? (
        <Notice tone="error">{t("plex.libraries.loadError")}</Notice>
      ) : syncLibraries.isError && libs.length === 0 ? (
        /* The read landed empty and the sync that would fill it failed, which is what an
           unlinked or unreachable Plex answers. Without this the step drew an empty grid and
           said nothing at all, so "no libraries" and "we never got to look" were the same
           picture (rule 17/36, rule 93). */
        <Notice tone="error">{t("setup.plex.librariesLoadErrorRetry")}</Notice>
      ) : (
        <>
          {libraries.isError && <StaleReadNotice what={t("plex.stale.libraries")} />}
          <div className="lib-grid">
            {libs.map((l) => (
              <div key={l.key} className={l.enabled ? "lib-card" : "lib-card off"}>
                <span>
                  {l.title}
                  <span className="lib-kind">
                    {l.kind === "movie"
                      ? t("plex.libraries.kindMovie")
                      : t("plex.libraries.kindTv")}
                  </span>
                </span>
                <Switch
                  checked={l.enabled}
                  ariaLabel={l.title}
                  disabled={setLibraries.isPending}
                  onChange={(on) => toggleLibrary(l.key, on)}
                />
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
