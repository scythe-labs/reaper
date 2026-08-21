import { Trans, useTranslation } from "react-i18next";
import { ApiError, type Snapshot } from "../api";
import { count, date } from "../format";
import { Notice } from "./Notice";

/** A slim freshness line on the Review screen: when the queue was last built, and a loud
 *  note if that scan came back incomplete (the scan control itself now lives in Settings →
 *  Jobs). Without this, the queue gives no sense of how stale it might be.
 *
 *  Missing data is not the same as "no scan exists". `/api/snapshots/latest` answers 404
 *  only for the genuine first-boot case; every other failure also arrives with no data, and
 *  reading that as "no scan has run yet" turns a dropped request into a confident claim and
 *  silently drops the incomplete-scan warning, the one staleness signal on this screen.
 *  Tested directly; the app renders it only from `App`'s `Dashboard`. */
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
    return <p className="scan-freshness muted">{t("shell.scanFreshness.checking")}</p>;
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
    // No "reload to try again" (#195): this line renders above a working review queue, where
    // `selected` is component state -- only the filters persist -- so a reload drops a bulk
    // selection that "Select everything matching" may have walked thousands of rows to build,
    // and nothing anywhere in the app asks first.
    return (
      // `standing`: this line is the queue's age, so it is part of the page whenever the read
      // behind it will not answer. It arrives that way on a first load, and again whenever
      // `useScanSettled` invalidates `["snapshot"]` off the shell's 15s poll -- a scheduled scan
      // finishing, with nothing pressed. It sits directly above the queue it describes.
      <Notice tone="error" className="scan-freshness" standing>
        {t("shell.scanFreshness.readFailed")}
      </Notice>
    );
  }
  return (
    <p className="scan-freshness muted">
      {t("shell.scanFreshness.lastScanned", {
        date: date(snapshot.created_at),
        count: count(snapshot.item_count),
      })}
      {snapshot.degraded && (
        <>
          {" "}
          {/* The separating period is OUTSIDE the notice. Inside it, the character that
              ends the neutral sentence before it painted amber and semibold, so the line
              opened on a floating yellow dot a word early.

              The shared `Notice`, not a bare styled span: it carries the visually-hidden
              "Warning: " lead, so severity is not amber alone. As a span this read as
              ordinary muted text to anyone not seeing the color, on the page where
              approvals are made (rules 18, 72).

              It says what the incomplete scan MEANS before it says what to do. The
              consequence clause was dropped here and in `ScanBar`, leaving `ReapPlan` the
              only one of the three still carrying it -- on the page an operator reaches
              last, while this line sits above the queue they are approving from. Whether it
              appeared at all had also become a matter of which source failed, since only
              `library_index`'s reasons bake it into their own text (rule 144).

              `standing`, and silent, because this is the age of the snapshot the queue below
              is built from: it is on the page for as long as that snapshot is the one on
              hand, not a reply to anything the operator pressed. The scan that produces it
              announces itself from `ScanBar`, which is where the transition happens. */}
          <Notice as="span" tone="warn" standing className="freshness-warn">
            {/* The only one of the three with no remedy in it, which is why this is the one
                that grew a link (rule 72). `ScanBar`'s copy renders ON Settings → Jobs,
                beside the Scan library button, so it would point at itself; `ReapPlan`'s
                already ends "Fix the source and scan again" on a page whose remedy is the
                source, not a rescan. Both were read and left as they are. */}
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
