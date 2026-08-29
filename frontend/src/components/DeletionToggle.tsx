// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The deletion switch: whether Reaper is allowed to remove anything at all.
//
// Extracted from the Settings safety panel so the Policy workspace can carry it as the
// last step of the decision pipeline. Turning deletion ON always takes the admin
// password. Turning it OFF never does, because the off direction can only make Reaper
// safer. This is safety UI, so it never renders nothing. While the state is unknown it
// says so, in the amber "we could not look" tone, never in a way that reads as safe.
//
// **It says which way it went, out loud.** Both directions call `announce()`, so an operator
// driving by ear hears whether the app just armed to delete their library. Focus goes back to
// the button that opened the form on both of its exits, the confirm and the cancel, because the
// form takes the focused element with it when it unmounts.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api } from "../api";
import { describeError } from "../errors";
import { useSafety } from "../useSafety";
import { Notice } from "./Notice";

export function DeletionToggle() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useSafety();
  const [confirming, setConfirming] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Whichever button is holding the row's action slot. It reads "Turn on…" before arming and
  // "Turn off" after. One ref covers both, since it is one control position. What the operator
  // wants back is the place they left, and after arming that place says the opposite thing.
  const rowButtonRef = useRef<HTMLButtonElement>(null);
  // Set when the password form is about to close and focus has to follow it somewhere. Read in
  // the effect below rather than acted on inline, because the button does not exist until the
  // commit that unmounts the form has landed. `.focus()` on a node that is not there yet is a
  // silent no-op, which is exactly what leaves focus on `<body>`.
  const returnToRow = useRef(false);
  useEffect(() => {
    if (!returnToRow.current || confirming) return;
    returnToRow.current = false;
    rowButtonRef.current?.focus();
  }, [confirming]);
  /** Close the password form and put the operator back on the button that opened it. */
  const closeForm = () => {
    returnToRow.current = true;
    setConfirming(false);
    setPassword("");
  };

  // ["safety"] is the whole list. It is the one query that carries whether deletion is on,
  // and every surface that gates on it (this switch, the app banner, the Reap page's
  // Execute button) reads it through useSafety.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["safety"] });
  };
  const toggle = useMutation({
    mutationFn: (vars: { enabled: boolean; password?: string }) =>
      api.setDeletion(vars.enabled, vars.password),
    onSuccess: (_data, vars) => {
      setPassword("");
      setConfirming(false);
      setError(null);
      // The same two sentences the state block above shows, said out loud, so keep them reading
      // alike. This announces from the settled mutation, never at issuance. This is the switch
      // that decides whether Reaper may delete, so a premature "on" would be the worst possible
      // thing to be wrong about.
      announce(vars.enabled ? t("deletion.stateOn") : t("deletion.stateOff"));
      // Arming happens from the password form, which unmounts on success and takes the focused
      // Confirm button with it. Without this flag, focus would land on `<body>` and the next
      // Tab press would restart at the top of the page.
      if (vars.enabled) returnToRow.current = true;
      refresh();
    },
    onError: (e) => setError(describeError(e)),
  });

  if (isLoading) {
    return <p className="muted">{t("common.checkingDeletion")}</p>;
  }
  // Unknown must never read as safe. Say it plainly, in amber, and still offer OFF, since OFF
  // needs no password and no prior state and can only make Reaper safer. That gives the operator
  // who wants read-only right now something to click. ON stays hidden here, since it takes a
  // password, and offering it against a state Reaper could not read would be arming on a guess.
  if (isError || !data) {
    return (
      <>
        {/* Only a toggle that turned deletion OFF may claim read-only here. `isSuccess` alone
            records that a toggle landed, but not which way it went. If a successful ARM's
            follow-up read then failed, checking `isSuccess` alone would paint the green
            "read-only" block over a host that was actually armed, an always-visible surface
            stating the opposite of the truth in the reassuring direction. `SafetyBanner.tsx`
            reads this state correctly, and the two sit on screen together. */}
        {toggle.isSuccess && toggle.variables?.enabled === false ? (
          <div className="safety-state safe">
            <span className="banner-dot" aria-hidden="true" />
            <div>
              <strong>{t("deletion.stateOff")}</strong>
            </div>
          </div>
        ) : (
          <Notice tone="warn">{t("deletion.unknownNotice")}</Notice>
        )}
        <div className="safety-row">
          <div>
            <strong>{t("deletion.turnOffHeading")}</strong>
            <p className="help">{t("deletion.turnOffHelpUnknown")}</p>
          </div>
          <button
            className="ghost danger"
            disabled={toggle.isPending}
            onClick={() => toggle.mutate({ enabled: false })}
          >
            {toggle.isPending ? t("deletion.turningOff") : t("deletion.turnOff")}
          </button>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
      </>
    );
  }

  const on = data.destructive_enabled;

  return (
    <>
      <div className={`safety-state ${on ? "armed" : "safe"}`}>
        <span className="banner-dot" aria-hidden="true" />
        <div>
          <strong>{on ? t("deletion.stateOn") : t("deletion.stateOff")}</strong>
        </div>
      </div>

      <div className="safety-row">
        <div>
          <strong>{on ? t("deletion.turnOffHeading") : t("deletion.turnOnHeading")}</strong>
          <p className="help">{on ? t("deletion.turnOffHelp") : t("deletion.turnOnHelp")}</p>
        </div>
        {on ? (
          <button
            ref={rowButtonRef}
            className="ghost danger"
            onClick={() => toggle.mutate({ enabled: false })}
          >
            {t("deletion.turnOff")}
          </button>
        ) : !data.has_password ? (
          <span className="muted">{t("common.noAdminPassword")}</span>
        ) : confirming ? (
          <form
            className="pw-form"
            onSubmit={(e) => {
              e.preventDefault();
              setError(null);
              toggle.mutate({ enabled: true, password });
            }}
          >
            {/* The placeholder is only a hint. It disappears the moment you type, so the
                `aria-label` is what names this field. */}
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              maxLength={128}
              placeholder={t("common.adminPasswordPlaceholder")}
              aria-label={t("common.adminPassword")}
              autoComplete="current-password"
              autoFocus
            />
            <button type="submit" className="primary sm" disabled={!password || toggle.isPending}>
              {t("deletion.confirmButton")}
            </button>
            {/* Cancel clears the typed password along with the form state. Without this, the
                password would stay in component state for as long as this panel is mounted,
                and would refill the field the next time it opens. */}
            <button type="button" className="ghost sm" onClick={closeForm}>
              {t("common.cancel")}
            </button>
          </form>
        ) : (
          <button ref={rowButtonRef} className="primary" onClick={() => setConfirming(true)}>
            {t("deletion.turnOnButton")}
          </button>
        )}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
    </>
  );
}
