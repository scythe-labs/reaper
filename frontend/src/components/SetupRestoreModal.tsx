// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The wizard's restore door: the Settings restore flow, in a modal over the Connect step.
//
// Why here. An operator moving an existing Reaper to a new host has, until now, had to walk
// the whole wizard, reach the app, then go to Settings and replace everything they had just
// configured (#385). Restoring is the *alternative* to adding services one at a time, so the
// door belongs on the step that asks for them. It cannot be the first step:
// `restore/confirm` refuses outright without an admin password, and the first step is what
// creates one.
//
// It is a real `ModalShell`, unlike the step cards themselves: there IS a page behind it and
// there IS an opener to return focus to, which is exactly the contract the shell owns. The
// close guard is deliberately open, including mid-upload. Nothing on this card is lost that a
// second click cannot redo, and `RestoreFlow`'s unmount reclaims the staged-but-unconfirmed
// archive THIS card put there rather than abandoning it. So a close leaves nothing of ours
// stranded: what it does not reclaim is a staging another card has replaced ours with, which
// that card is still showing and still owns (#387). A confirm already in flight is waited on
// and whatever it armed is left alone, which is why closing during one is not a way to lose a
// restore either.

import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { ModalShell } from "./ModalShell";
import { Notice } from "./Notice";
import { RestoreFlow } from "./RestoreCard";

export function SetupRestoreModal({
  password,
  onClose,
}: {
  /** The password from step one, so the confirm does not ask for it again. Null when the
   *  wizard was resumed in a fresh tab and never held it, and the flow draws its box. */
  password: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  // `armed` is server state, so it has to be read rather than assumed: a restore staged from
  // another tab, or before this browser was opened, must show as staged here too.
  const { data, isPending, isError } = useQuery({
    queryKey: ["backup-info"],
    queryFn: api.backupInfo,
  });

  return (
    <ModalShell title={t("backup.restore.heading")} onClose={onClose}>
      {/* Three states, none of them silence (rule 17/36). A failed read must not fall through
          to the idle card: that card would invite an upload whose confirm is about to be
          refused, and it would hide an armed restore that is already staged. */}
      {isPending && <p className="muted">{t("common.loading")}</p>}
      {/* Close and reopen, never "reload the page". A reload is the app-wide advice and it is
          wrong from inside the wizard: the password from step one lives in React state, so a
          reload would drop it and this door would go back to asking for it. Reopening the
          modal refetches, which is the whole of what a reload would have achieved. */}
      {isError && !data && <Notice tone="error">{t("setup.restore.checkFailedError")}</Notice>}
      {data && <RestoreFlow armed={data.restore_armed} heldPassword={password} />}
    </ModalShell>
  );
}
