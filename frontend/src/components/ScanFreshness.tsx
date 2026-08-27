import { Trans, useTranslation } from "react-i18next";
import { ApiError, type Snapshot } from "../api";
import { count, since } from "../format";
import { Notice } from "./Notice";

/** A slim freshness line on the Review screen: when the queue was last built, and a loud
 *  note if that scan came back incomplete. (The scan control itself lives in Settings →
 *  Jobs.) Without this, the queue gives no sense of how stale it might be.
 *
 *  Missing data is not the same as "no scan exists". `/api/snapshots/latest` answers 404
 *  only for the genuine first-boot case. Every other failure also arrives with no data, and
 *  reading that as "no scan has run yet" turns a dropped request into a confident claim,
 *  silently dropping the incomplete-scan warning, the one staleness signal on this screen.
 *  Tested directly, since the app renders it only from `App`'s `Dashboard`. */
export function ScanFreshness({
  snapshot,
  isPending,
  error,
  onGoToJobs,
}: {
  snapshot: Snapshot | undefined;
  isPending: boolean;
  error: unknown;
  onGoToJobs: () => void;
}) {
  const { t } = useTranslation();
  if (isPending) {
    return <p className="scan-freshness muted">{t("common.checkingLastScan")}</p>;
  }
  if (!snapshot) {
    if (error instanceof ApiError && error.status === 404) {
      return (
        <p className="scan-freshness muted">
          <Trans
            i18nKey="shell.scanFreshness.noScanYet"
            components={{ btn: <button className="link" onClick={onGoToJobs} /> }}
          />
        </p>
      );
    }
    // Never suggest reloading to try again here. This line renders above a working review
    // queue where `selected` is component state (only the filters persist), so a reload
    // drops a bulk selection that "Select everything matching" may have walked thousands of
    // rows to build, with no confirmation asked first.
    return (
      // `standing`: this notice is the queue's age, so it is part of the page whenever the
      // read behind it fails to answer. That happens on first load, and again whenever
      // `useScanSettled` invalidates `["snapshot"]` off the shell's 15s poll, such as a
      // scheduled scan finishing with nothing pressed. It sits directly above the queue it
      // describes.
      <Notice tone="error" className="scan-freshness" standing>
        {t("shell.scanFreshness.readFailed")}
      </Notice>
    );
  }
  return (
    <p className="scan-freshness muted">
      {t("shell.scanFreshness.lastScanned", {
        when: since(snapshot.created_at),
        count: count(snapshot.item_count),
      })}
      {snapshot.degraded && (
        <>
          {" "}
          {/* The separating period sits outside the notice. Inside it, the same character
              would end the neutral sentence in the notice's own amber, semibold style, so
              the line would open on a floating yellow dot one word early.

              This uses the shared `Notice`, not a bare styled span: it carries a
              visually-hidden "Warning: " lead, so severity is not carried by color alone. A
              plain span would read as ordinary muted text to anyone who can't see the
              color, on the page where approvals happen.

              This states what the incomplete scan means before it says what to do. Of the
              three places that make this claim, only `ReapPlan` still states the
              consequence directly, on the page an operator reaches last, while this line
              sits above the queue they are approving from. Otherwise, whether the
              consequence shows up at all depends on which source failed: only
              `library_index`'s reasons bake it into their own text.

              `standing`, and silent, because this is the age of the snapshot the queue
              below is built from. It stays on the page for as long as that snapshot is the
              one on hand, not as a reply to anything the operator pressed. The scan that
              produces it announces itself from `ScanBar`, where that transition happens. */}
          <Notice as="span" tone="warn" standing className="freshness-warn">
            {/* This is the only one of the three with no remedy of its own, which is why it
                is the one that grew a link. `ScanBar`'s copy renders on Settings → Jobs,
                beside the Scan now button, so a link there would point at itself.
                `ReapPlan`'s already ends "Fix the source and scan again" on a page whose
                remedy is the source, not a rescan. */}
            <Trans
              i18nKey="shell.scanFreshness.degradedNotice"
              components={{ btn: <button className="link" onClick={onGoToJobs} /> }}
            />
          </Notice>
        </>
      )}
    </p>
  );
}
