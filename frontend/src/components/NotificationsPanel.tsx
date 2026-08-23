// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Notifications: where Reaper says what it did. Discord only, one webhook URL.
//
// The URL is a secret the operator has to go back to Discord to re-copy, so an unsaved one is
// worth more than an unsaved setting: the draft reports upward through `onDirtyChange` and a
// section switch asks first (rule 146).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type RefObject, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import { languageName } from "../i18n";
import { useSuccessorFocus } from "../focus";
import { api } from "../api";
import { describeError } from "../errors";
import { TestBadge, useWebhookTest } from "./ServiceModal";
import { SetRow } from "./SetRow";
import { StaleReadNotice } from "./StaleReadNotice";
import { Notice } from "./Notice";

/** The webhook box's format complaint, named once for both ends (rule 67). */
const WEBHOOK_ERROR_ID = "discord-webhook-error";

/** The Discord webhook is the only channel that actually warns your users before a title
 *  is deleted -- the Plex "Leaving Soon" label only reaches people who pinned the library. It
 *  is a write-only secret: the URL is sent once, encrypted on arrival, and never comes back,
 *  so the field is always blank and we report only *whether* a webhook is connected. Same
 *  pattern as an instance API key. */
// Exported for TestBadgeFreshness.test.tsx, which drives this row's badge against an edited URL.
export function NotificationsPanel({
  /** Called whenever the webhook box gains or loses a draft, so the section rail can hold a
   *  switch that would discard one. Pass a STABLE function: it is an effect dependency. */
  onDirtyChange,
}: {
  onDirtyChange?: ((dirty: boolean) => void) | undefined;
} = {}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
  });
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const connected = data?.has_webhook ?? false;
  const {
    typed,
    validNew,
    badFormat,
    canTest,
    test,
    testedWith,
    clearTest,
    sendTest: testWebhook,
  } = useWebhookTest(url, connected, (e) => setError(describeError(e)));

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["notifications"] });

  const save = useMutation({
    mutationFn: () => api.setWebhook(url.trim()),
    onSuccess: () => {
      setUrl("");
      clearTest();
      setError(null);
      // Success here is the box emptying and a line above it flipping, both silent. The test
      // button between these two mutations already speaks (#192); these are its siblings.
      announce(t("services.discord.savedAnnouncement"));
      invalidate();
    },
    onError: (e) => setError(describeError(e)),
  });
  // The language select writes immediately, like GeneralPanel's expand-seasons and
  // reverse-proxy selects -- there is nothing to lose by leaving it, so it needs no place in
  // a draft or a save bar (rule 43 does not apply: this panel has no bar to begin with).
  const saveLanguage = useMutation({
    mutationFn: (language: string) => api.setNotificationLanguage(language),
    onSuccess: () => {
      announce(t("services.notifications.language.savedAnnouncement"));
      invalidate();
    },
    onError: (e) => setError(describeError(e)),
  });
  // Remove is the rule 72 twin of the API key's, and the harder half of the pair: removing the
  // webhook disables BOTH of the pressed button's siblings in the same breath -- Save wants a
  // typed URL and `setUrl("")` has just cleared it, Send test wants a stored one and that is what
  // went -- so there is no successor control at all, only the box the operator would refill.
  // Which makes it the honest target: it is the one thing left to do here (#173).
  const afterWebhookRemove = useSuccessorFocus();
  const remove = useMutation({
    mutationFn: () => api.clearWebhook(),
    onSuccess: () => {
      setUrl("");
      clearTest();
      setError(null);
      announce(t("services.discord.removedAnnouncement"));
      invalidate();
    },
    onError: (e) => setError(describeError(e)),
  });

  // What this panel would LOSE, reported up to `Settings` so leaving the section can stop and ask
  // first. A pasted webhook is the costliest draft in Settings to drop: it is a secret, it is
  // never shown again once stored, and re-typing it means going back to Discord for it.
  //
  // Rule 146 asks two things of this signal, and here they are the same fact. There is something
  // to lose exactly when the box holds text, and the box is reachable in EVERY state this panel
  // renders: it has no early return, and the loading and failed-check branches above swap only
  // the one status line over it, never the box, its help, or its Save. `typed` rather than
  // `validNew`, because a half-pasted URL that Save refuses is still a draft that leaving throws
  // away -- reporting only the saveable form would drop the malformed one silently.
  useEffect(() => {
    onDirtyChange?.(typed);
  }, [typed, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  return (
    <div className="panel">
      <h2>{t("services.notifications.heading")}</h2>
      <p className="blurb">{t("services.notifications.blurb")}</p>

      {/* Whether the warning channel exists is only worth stating once it has been read:
          an unread answer must not claim that nobody is being warned.

          Three branches, not two (rule 17/36, rule 72). React Query keeps the last good answer
          through a failed refetch -- which `save` and `remove` both trigger, since each
          invalidates this key on success -- and raises the failure beside it. This panel has no
          early return, so the "couldn't check" sentence printed directly above three controls
          derived from that very answer, each of them acting as though it HAD been checked: the
          "leave blank to keep the current webhook" placeholder, an enabled Remove, and a Send
          test that fires at the stored webhook. The same sentence also rendered over the opposite
          form when the FIRST read failed, so the two states could not be told apart.

          Neither branch says to reload (#195). The panel has no early return, so the webhook box
          below is on screen in EVERY branch, and what is typed into it is a secret Reaper stores
          encrypted and never shows again -- a reload costs the operator a value they have to go
          back to Discord for, and nothing anywhere in `frontend/src` asks first. That is the same
          harm #153 took off the shared line; this sentence is hand-written, so it kept it. */}
      {isPending ? (
        <p className="muted">{t("services.notifications.checking")}</p>
      ) : isError && !data ? (
        <Notice tone="error">{t("services.notifications.checkError")}</Notice>
      ) : (
        <>
          {isError && <StaleReadNotice what={t("services.notifications.staleWhat")} />}
          {connected ? (
            <p className="muted">
              {/* The sentence says the state in words either way, so the tick would only
                  interrupt it with a stray character -- the same call `:1467`'s `.dot` ✓ makes
                  a few hundred lines above, and the one #177 made for the `.gate-mark` pair. */}
              <span aria-hidden="true">✓</span> {t("services.notifications.connected")}
            </p>
          ) : (
            <p className="muted">{t("services.notifications.notConnected")}</p>
          )}
        </>
      )}

      {/* Always on screen, like the webhook box below: a loading or failed check narrows to
          "en" and disables the select rather than hiding it (rule 17/36). */}
      <SetRow
        label={t("services.notifications.language.label")}
        help={t("services.notifications.language.help")}
      >
        <select
          value={data?.language ?? "en"}
          aria-label={t("services.notifications.language.label")}
          disabled={isPending || saveLanguage.isPending}
          onChange={(e) => {
            setError(null);
            saveLanguage.mutate(e.target.value);
          }}
        >
          {(data?.languages ?? ["en"]).map((tag) => (
            <option key={tag} value={tag}>
              {languageName(tag, "en")}
            </option>
          ))}
        </select>
      </SetRow>

      <div className="add-grid">
        <label className="field-sm wide">
          <span className="field-label">{t("services.notifications.field.label")}</span>
          <input
            type="password"
            ref={afterWebhookRemove.ref as RefObject<HTMLInputElement>}
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              setError(null);
            }}
            placeholder={
              connected
                ? t("services.notifications.field.placeholderEdit")
                : t("services.notifications.field.placeholderAdd")
            }
            autoComplete="off"
            // The complaint renders after the whole button row, so in DOM order it is three
            // controls away from the box it is about (#174).
            aria-invalid={badFormat ? true : undefined}
            aria-describedby={badFormat ? WEBHOOK_ERROR_ID : undefined}
          />
        </label>
      </div>
      <p className="help">{t("services.notifications.field.help")}</p>

      <div className="add-actions">
        <button
          type="button"
          className="primary"
          disabled={!validNew || save.isPending}
          onClick={() => {
            setError(null);
            save.mutate();
          }}
        >
          {save.isPending ? t("common.saving") : t("common.save")}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={!canTest}
          onClick={() => {
            setError(null);
            clearTest();
            testWebhook.mutate();
          }}
        >
          {testWebhook.isPending
            ? t("services.common.testing")
            : t("services.discord.sendTestButton")}
        </button>
        {connected && (
          <button
            type="button"
            className="ghost danger"
            disabled={remove.isPending}
            onClick={() => {
              setError(null);
              afterWebhookRemove.arriving();
              remove.mutate();
            }}
          >
            {remove.isPending ? t("common.removing") : t("common.remove")}
          </button>
        )}
        <TestBadge result={test && test.of === testedWith() ? test.result : null} />
      </div>
      {badFormat && (
        <Notice tone="error" id={WEBHOOK_ERROR_ID}>
          {t("services.discord.badFormat")}
        </Notice>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </div>
  );
}
