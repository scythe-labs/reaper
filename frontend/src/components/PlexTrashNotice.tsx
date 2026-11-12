// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one notice about what Plex's trash holds before a reap.

import { Trans, useTranslation } from "react-i18next";
import { Notice } from "./Notice";

/** Says what happens to the items already in Plex's trash when this reap runs.
 *
 * `plexEmpties` is Plex's empty-after-scan preference. `true` means Plex removes those
 * items' records. `false` means the trash is left alone, so the reaped titles stay in Plex
 * as unavailable. `null` means the preference could not be read.
 *
 * The checkbox renders only with `onAck`. The caller passes it when the operator must tick
 * before Reap enables.
 */
export function PlexTrashNotice({
  known,
  unreadable,
  plexEmpties,
  acked,
  onAck,
}: {
  known: number;
  unreadable: boolean;
  plexEmpties: boolean | null;
  acked?: boolean;
  onAck?: ((next: boolean) => void) | undefined;
}) {
  const { t } = useTranslation();
  // `known` is an exact count. A library that could not be read makes it a floor, and no
  // count at all is its own wording.
  const read = known > 0 ? (unreadable ? "partial" : "known") : "unread";
  const fill = { values: { n: known }, components: { strong: <strong /> } };
  return (
    // `standing`: this mounts on a Plex read, never on a press, so it must not interrupt.
    <Notice tone="warn" standing>
      {plexEmpties === true ? (
        read === "known" ? (
          <Trans i18nKey="plex.trashNotice.emptiesKnown" {...fill} />
        ) : read === "partial" ? (
          <Trans i18nKey="plex.trashNotice.emptiesPartial" {...fill} />
        ) : (
          <Trans i18nKey="plex.trashNotice.emptiesUnread" {...fill} />
        )
      ) : plexEmpties === false ? (
        read === "known" ? (
          <Trans i18nKey="plex.trashNotice.keptKnown" {...fill} />
        ) : read === "partial" ? (
          <Trans i18nKey="plex.trashNotice.keptPartial" {...fill} />
        ) : (
          <Trans i18nKey="plex.trashNotice.keptUnread" {...fill} />
        )
      ) : read === "known" ? (
        <Trans i18nKey="plex.trashNotice.mayEmptyKnown" {...fill} />
      ) : read === "partial" ? (
        <Trans i18nKey="plex.trashNotice.mayEmptyPartial" {...fill} />
      ) : (
        <Trans i18nKey="plex.trashNotice.mayEmptyUnread" {...fill} />
      )}
      {onAck && (
        <label className="trash-ack">
          <input type="checkbox" checked={!!acked} onChange={(e) => onAck(e.target.checked)} />
          <span>{t("plex.trashNotice.ackLabel")}</span>
        </label>
      )}
    </Notice>
  );
}
