// SPDX-License-Identifier: AGPL-3.0-or-later
// Each Sonarr and Radarr instance's recycle bin: the list on the reap confirm, and the one on a
// past run's report. A delete through an instance with a bin frees its space only when the bin
// empties.
import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { api, type InstanceKind, type RunBin } from "../api";
import { bytes, count } from "../format";
import { kindLabel } from "./ServiceModal";

/** A run's bins. For a planned run the server reads them live, for any other it returns what
 *  the run recorded as it started. */
export function useRunBins(runId: number) {
  return useQuery({
    queryKey: ["run-bins", runId],
    queryFn: () => api.runBins(runId),
    // Read once per sheet. A planned run's bins are read live when the confirm opens, and a
    // finished run's are stored and never change.
    staleTime: Infinity,
  });
}

/** Bytes a run freed now: everything it removed, minus what waits in a recycle bin. */
export function freedNow(deleted: number, binned: number | null | undefined): number {
  return deleted - (binned ?? 0);
}

/** The longest any bin that is on keeps a file. `null` when an on bin has no cleanup days to
 *  promise. `undefined` when no bin is on or one could not be read, so no date is known. */
export function binDays(bins: RunBin[]): number | null | undefined {
  let days: number | undefined;
  for (const b of bins) {
    if (b.bin === "unknown") return undefined;
    if (b.bin !== "on") continue;
    if (!b.cleanup_days) return null;
    days = Math.max(days ?? 0, b.cleanup_days);
  }
  return days;
}

/** The recycle bin list on the reap confirm: one row per instance, then when the space frees. */
export function ConfirmBins({ runId }: { runId: number }) {
  const { t } = useTranslation();
  const { data, isPending } = useRunBins(runId);
  if (isPending) return null;
  if (!data) return <p className="help bins-failed">{t("recycleBins.loadFailed")}</p>;
  const bins = data.bins;
  if (!bins.some((b) => b.bin === "on" || b.bin === "unknown")) return null;

  const now = bins.filter((b) => b.bin === "none").reduce((sum, b) => sum + b.bytes, 0);
  const later = bins.filter((b) => b.bin === "on").reduce((sum, b) => sum + b.bytes, 0);
  const days = binDays(bins);
  const summary: string[] = [];
  if (!bins.some((b) => b.bin === "unknown")) {
    if (now > 0) summary.push(t("recycleBins.summaryNow", { bytes: bytes(now) }));
    if (later > 0)
      summary.push(
        days == null
          ? t("recycleBins.summaryLaterWhenEmptied", { bytes: bytes(later) })
          : t("recycleBins.summaryLater", { bytes: bytes(later), count: count(days), n: days }),
      );
  }

  return (
    <section className="bins" aria-labelledby="bins-heading">
      <h3 id="bins-heading" className="bins-head">
        {t("recycleBins.heading")}
      </h3>
      <p className="bins-help">{t("recycleBins.help")}</p>
      <ul className="bin-list">
        {bins.map((b) => (
          <li key={`${b.kind}:${b.instance_id}`} className={`bin-row bin-${b.bin}`}>
            <span className="bin-app">
              <InstanceName bin={b} />
            </span>
            <span className="bin-what">
              {b.kind === "sonarr"
                ? t("recycleBins.seasons", {
                    count: count(b.items),
                    n: b.items,
                    bytes: bytes(b.bytes),
                  })
                : t("recycleBins.movies", {
                    count: count(b.items),
                    n: b.items,
                    bytes: bytes(b.bytes),
                  })}
            </span>
            <span className="bin-when">{whenText(b, t)}</span>
          </li>
        ))}
      </ul>
      {summary.length > 0 && <p className="bins-sum">{summary.join(" ")}</p>}
    </section>
  );
}

/** The services list's label for an instance: its kind, then the name the operator gave it,
 *  since two kinds often share a name ("HD"). */
function InstanceName({ bin }: { bin: RunBin }) {
  return (
    <>
      <span className={`kind-badge kind-${bin.kind}`}>{kindLabel(bin.kind as InstanceKind)}</span>{" "}
      <strong>{bin.name}</strong>
    </>
  );
}

function whenText(b: RunBin, t: TFunction): string {
  if (b.bin === "none") return t("recycleBins.noBin");
  if (b.bin === "unknown") return t("recycleBins.unknown");
  return b.cleanup_days
    ? t("recycleBins.freesIn", { count: count(b.cleanup_days), n: b.cleanup_days })
    : t("recycleBins.freesWhenEmptied");
}

/** The recycle bin list on a past run's report: each instance, and what it did with the files. */
export function RunBinsList({ runId }: { runId: number }) {
  const { t } = useTranslation();
  const query = useRunBins(runId);
  if (!query.data || query.data.bins.length === 0) return null;
  return (
    <>
      <h3 className="reap-feed-heading">{t("recycleBins.heading")}</h3>
      <div className="feed run-bins">
        {query.data.bins.map((b) => (
          <div
            key={`${b.kind}:${b.instance_id}`}
            className={b.bin === "unknown" ? "feed-row kept" : "feed-row gone"}
          >
            <span className="feed-mark" aria-hidden="true">
              {b.bin === "unknown" ? "?" : "✓"}
            </span>
            <span className="feed-title">
              <InstanceName bin={b} /> <span className="feed-kept-why">{reportText(b, t)}</span>
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

function reportText(b: RunBin, t: TFunction): string {
  if (b.bin === "none") return t("recycleBins.report.noBin");
  if (b.bin === "unknown") return t("recycleBins.report.unknown");
  return b.cleanup_days
    ? t("recycleBins.report.on", {
        bytes: bytes(b.bytes),
        count: count(b.cleanup_days),
        n: b.cleanup_days,
      })
    : t("recycleBins.report.onWhenEmptied", { bytes: bytes(b.bytes) });
}
