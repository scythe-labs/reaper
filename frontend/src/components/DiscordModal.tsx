// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Discord, in the shape every other connection wears.
//
// A connection should look like a connection wherever the operator meets it, so this is the
// service editor's layout down to the class names: `ModalShell`, the `kind-badge` in the
// title, one `.service-form` of `.field-sm` boxes, the shared `TestBadge`, and an
// `.add-actions` footer of Cancel / Test / Save. What it is NOT is a second `ServiceModal`:
// Discord is not an `Instance`, it has no host, port or API key, and it is stored as one
// encrypted settings key rather than a row -- so it borrows the grammar and keeps its own
// plumbing (rule 18: reuse the pattern, never fork the component to fit).
//
// The webhook is write-only, exactly like an instance's API key: the URL is sent once,
// encrypted on arrival, and never comes back, so the box is always blank and all Reaper can
// report is *whether* one is connected.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type InstanceTest } from "../api";
import { describeError } from "../errors";
import { composeIn } from "../why";
import { ModalShell } from "./ModalShell";
import { Notice } from "./Notice";
import { TestBadge, testSentence } from "./ServiceModal";
import { isDiscordWebhook } from "./Settings";

/** The webhook box's format complaint, named once for both ends of the association
 *  (rule 67). */
const WEBHOOK_ERROR_ID = "discord-modal-webhook-error";

export function DiscordModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["notifications"], queryFn: api.notifications });
  const connected = data?.has_webhook ?? false;

  const [url, setUrl] = useState("");
  // The result and the URL it was computed for, the pairing `ServiceModal` keeps for its own
  // badge (rule 72): without it a passed test then a pasted-over URL leaves "Passed" beside a
  // webhook nobody has sent to (rule 85).
  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  /** What a test would be sent. A blank box tests the webhook already STORED, so blank is a
   *  real value here rather than an absence. */
  const testedWith = () => url.trim();

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

  const sendTest = useMutation({
    // Test what is typed if anything is; otherwise test what is stored, so a saved channel can
    // be verified without going back to Discord for the URL again.
    mutationFn: () => api.testWebhook(url.trim() ? url.trim() : null),
    // What this request is ABOUT, captured when it is issued rather than computed at success
    // time, the same as `ServiceModal`'s own test (rule 72). The box stays live while the request
    // is out. `testedWith()` called at success would fingerprint whatever is typed by then, so a
    // second webhook pasted mid-send gets "Passed" for a channel nobody tried (rule 85).
    onMutate: () => ({ of: testedWith() }),
    onSuccess: (r, _v, issued) => {
      // The server sends a typed reason (docs/history/I18N_PLAN.md §5); composed here, once,
      // under its own `services.discord.testResult` namespace (never `services.test`, which
      // is `ServiceModal.tsx`'s own), then carried as a `legacy` reason so `TestBadge` and
      // `testSentence` render the already-composed sentence verbatim rather than re-routing
      // it through a namespace it does not belong to.
      const result: InstanceTest = {
        ok: r.ok,
        detail_reason: {
          k: "legacy",
          p: { text: composeIn("services.discord.testResult", r.reason) },
        },
        version: r.version,
      };
      setTest({ result, of: issued.of });
      announce(testSentence(result));
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

  const typed = url.trim().length > 0;
  const validNew = typed && isDiscordWebhook(url);
  const badFormat = typed && !validNew;
  const canTest = (validNew || (!typed && connected)) && !sendTest.isPending;
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
      // A close mid-save unmounts the only place a failure is ever shown, exactly as it would
      // in the service editor (rule 80: the scrim, Escape and the ✕ all run this one guard).
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

        {/* Beside the control that fixes it (rule 42), not in the shared slot at the foot
            where a failed save also lands. */}
        {badFormat && (
          <Notice tone="error" id={WEBHOOK_ERROR_ID}>
            {t("services.discord.badFormat")}
          </Notice>
        )}

        {/* Only while it still describes what is in the box (rule 85). */}
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
