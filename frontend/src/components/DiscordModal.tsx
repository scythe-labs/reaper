// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Discord, in the shape every other connection wears.
//
// A connection should look like a connection wherever the operator meets it. This component
// borrows the service editor's `ModalShell`, its `kind-badge` in the title, one `.service-form`
// of `.field-sm` boxes, the shared `TestBadge`, and an `.add-actions` footer of Cancel, Test,
// and Save.
//
// Discord is not a `ServiceModal` `Instance`. It has no host, port, or API key, and it is
// stored as one encrypted settings key rather than a row. So this component reuses the
// pattern's layout instead of forking a new component to fit it.
//
// The webhook is write-only, exactly like an instance's API key. The URL is sent once,
// encrypted on arrival, and never comes back, so the box stays blank, and all Reaper can
// report is *whether* one is connected.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api } from "../api";
import { describeError } from "../errors";
import { ModalShell } from "./ModalShell";
import { Notice } from "./Notice";
import { TestBadge, useWebhookTest } from "./ServiceModal";

/** The webhook box's format complaint, named once so the input's `aria-describedby` and the
 *  error `Notice`'s `id` point at the same element. */
const WEBHOOK_ERROR_ID = "discord-modal-webhook-error";

export function DiscordModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: api.notifications });
  const connected = data?.has_webhook ?? false;

  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { validNew, badFormat, canTest, test, testedWith, sendTest } = useWebhookTest(
    url,
    connected,
    (e) => setError(describeError(e)),
  );

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["notifications"] });

  const save = useMutation({
    mutationFn: () => api.setWebhook(url.trim()),
    onSuccess: async () => {
      await invalidate();
      announce(t("services.discord.savedAnnouncement"));
      onClose();
    },
    onError: (e) => setError(describeError(e)),
  });

  const remove = useMutation({
    mutationFn: () => api.clearWebhook(),
    onSuccess: async () => {
      await invalidate();
      announce(t("services.discord.removedAnnouncement"));
      onClose();
    },
    onError: (e) => setError(describeError(e)),
  });

  const busy = save.isPending || remove.isPending;

  return (
    <ModalShell
      title={
        <>
          <span className="kind-badge kind-discord">{t("common.brand.discord")}</span>{" "}
          {connected ? t("services.discord.titleEdit") : t("services.discord.titleAdd")}
        </>
      }
      onClose={onClose}
      // A close mid-save would unmount the only place a failure is ever shown, the same risk
      // the service editor guards against. The scrim, Escape, and the close button all run
      // this one guard.
      canClose={!busy}
      className="service-modal"
    >
      <p className="blurb">{t("services.discord.blurb")}</p>

      <form
        className="service-form"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          if (validNew && !busy) save.mutate();
        }}
      >
        <label className="field-sm">
          <span className="field-label">{t("services.discord.field.webhookUrl")}</span>
          <input
            type="password"
            autoComplete="off"
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              setError(null);
            }}
            placeholder={
              connected
                ? t("services.discord.field.placeholderEdit")
                : t("services.discord.field.placeholderAdd")
            }
            aria-invalid={badFormat ? true : undefined}
            aria-describedby={badFormat ? WEBHOOK_ERROR_ID : undefined}
          />
          <span className="help">{t("services.discord.field.help")}</span>
        </label>

        {/* This sits beside the field it explains. The failed-save notice below uses a
            separate slot at the form's foot. */}
        {badFormat && (
          <Notice tone="error" id={WEBHOOK_ERROR_ID}>
            {t("services.discord.badFormat")}
          </Notice>
        )}

        {/* Shows only while the test result still describes what is currently in the box. */}
        {test && test.of === testedWith() && <TestBadge result={test.result} />}

        {error && <Notice tone="error">{t("services.discord.saveError", { error })}</Notice>}

        <div className="add-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </button>
          <span className="flex-spacer" />
          {connected && (
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setError(null);
                remove.mutate();
              }}
              disabled={busy}
            >
              {remove.isPending ? t("common.removing") : t("common.remove")}
            </button>
          )}
          <button
            type="button"
            className="ghost"
            onClick={() => {
              setError(null);
              sendTest.mutate();
            }}
            disabled={!canTest || busy}
          >
            {sendTest.isPending
              ? t("services.discord.sendTestPending")
              : t("services.discord.sendTestButton")}
          </button>
          <button type="submit" className="primary" disabled={!validNew || busy}>
            {save.isPending ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </form>
    </ModalShell>
  );
}
