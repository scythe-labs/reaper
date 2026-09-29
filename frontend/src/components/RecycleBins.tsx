// SPDX-License-Identifier: AGPL-3.0-or-later
// Each Sonarr and Radarr instance's recycle bin: the list on the reap confirm, the one on a
// past run's report, and the banner for a bin a reap left off. A delete through an instance
// with a bin frees its space only when the bin empties.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { api, type InstanceKind, type RunBin } from "../api";
import { describeError } from "../errors";
import { bytes, count } from "../format";
import { Notice } from "./Notice";
import { kindLabel } from "./ServiceModal";

/** A run's bins. A finished run's are stored and never change, so they are read once per
 *  sheet. A planned run's are read from the servers each time, so `live` reads them again
 *  whenever the confirm opens. */
export function useRunBins(runId: number, live = false) {
  return useQuery({
    queryKey: ["run-bins", runId],
    queryFn: () => api.runBins(runId),
    staleTime: live ? 0 : Infinity,
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

/** The confirmation phrase once `skipped` bins are ticked off. The server builds the same
 *  suffix (`planner.confirmation_phrase`), and `test_the_phrase_names_the_bins_it_turns_off`
 *  pins that copy. */
export function skipPhrase(base: string, skipped: number): string {
  if (skipped === 0) return base;
  return `${base} SKIP ${skipped} ${skipped === 1 ? "BIN" : "BINS"}`;
}

/** One instance, as `kind:instance_id`. */
export function binKey(b: { kind: string; instance_id: number }): string {
  return `${b.kind}:${b.instance_id}`;
}

function instanceLabel(kind: string, name: string): string {
  return `${kindLabel(kind as InstanceKind)} ${name}`;
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

/** The recycle bin list on the reap confirm: one row per instance, a box to skip each bin that
 *  is on, then when the space frees. */
export function ConfirmBins({
  runId,
  skip,
  onSkip,
}: {
  runId: number;
  /** The ticked instances, by `binKey`. */
  skip: ReadonlySet<string>;
  onSkip: (key: string, skipped: boolean) => void;
}) {
  const { t } = useTranslation();
  const { data, isPending } = useRunBins(runId, true);
  if (isPending) return null;
  if (!data) return <p className="help bins-failed">{t("recycleBins.loadFailed")}</p>;
  const bins = data.bins;
  if (!bins.some((b) => b.bin === "on" || b.bin === "unknown")) return null;

  const skipped = (b: RunBin) => b.bin === "on" && skip.has(binKey(b));
  const waiting = bins.filter((b) => b.bin === "on" && !skipped(b));
  const now = bins
    .filter((b) => b.bin === "none" || skipped(b))
    .reduce((sum, b) => sum + b.bytes, 0);
  const later = waiting.reduce((sum, b) => sum + b.bytes, 0);
  const days = binDays(waiting);
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
        {bins.map((b) => {
          const key = binKey(b);
          return (
            <li key={key} className={`bin-row bin-${b.bin}${skipped(b) ? " skipped" : ""}`}>
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
              <span className="bin-when">
                {skipped(b) ? t("recycleBins.freesNowNoUndo") : whenText(b, t)}
              </span>
              {b.bin === "on" && (
                <label className="trash-ack bin-skip">
                  <input
                    type="checkbox"
                    checked={skipped(b)}
                    onChange={(e) => onSkip(key, e.target.checked)}
                    aria-label={t("recycleBins.skipFor", { name: instanceLabel(b.kind, b.name) })}
                  />
                  <span aria-hidden="true">{t("recycleBins.skip")}</span>
                </label>
              )}
            </li>
          );
        })}
      </ul>
      {summary.length > 0 && <p className="bins-sum">{summary.join(" ")}</p>}
      {bins.some(skipped) && (
        <Notice tone="warn" className="bins-warn">
          {t("recycleBins.skipWarning")}
        </Notice>
      )}
    </section>
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
  if (!query.isError && (!query.data || query.data.bins.length === 0)) return null;
  return (
    <>
      <h3 className="reap-feed-heading">{t("recycleBins.heading")}</h3>
      {query.isError && <p className="help bins-failed">{t("recycleBins.loadFailed")}</p>}
      <div className="feed run-bins">
        {query.data?.bins.map((b) => {
          const warn = b.bin === "unknown" || b.state === "off" || b.state === "turning_off";
          return (
            <div key={binKey(b)} className={warn ? "feed-row kept" : "feed-row gone"}>
              <span className="feed-mark" aria-hidden="true">
                {warn ? "!" : "✓"}
              </span>
              <span className="feed-title">
                <InstanceName bin={b} /> <span className="feed-kept-why">{reportText(b, t)}</span>
              </span>
            </div>
          );
        })}
      </div>
    </>
  );
}

function reportText(b: RunBin, t: TFunction): string {
  if (b.state === "restored") return t("recycleBins.report.skippedBackOn");
  if (b.state === "off" || b.state === "turning_off")
    return t("recycleBins.report.skippedStillOff");
  if (b.state === "left") return t("recycleBins.report.left");
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

/** A bin a reap turned off and could not put back. Shown across the app until it is back on,
 *  with a button that tries again. */
export function RecycleBinBanner() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const off = useQuery({ queryKey: ["recycle-bins-off"], queryFn: api.recycleBinsOff });
  const retry = useMutation({
    mutationFn: api.restoreRecycleBins,
    onSuccess: (result) => {
      queryClient.setQueryData(["recycle-bins-off"], result);
    },
  });
  const bins = off.data?.bins ?? [];
  if (bins.length === 0) return null;
  // The request worked and the bin is still off.
  const retried = retry.isSuccess && retry.data.bins.length > 0;
  return (
    <>
      {bins.map((b) => (
        <div key={binKey(b)} className="banner banner-unknown">
          <span className="banner-dot" aria-hidden="true" />
          <span>
            {retry.isError
              ? describeError(retry.error)
              : retried
                ? t("recycleBins.banner.retryFailed", { name: instanceLabel(b.kind, b.name) })
                : t("recycleBins.banner.stillOff", {
                    name: instanceLabel(b.kind, b.name),
                    run: b.run_id,
                  })}{" "}
            <button
              type="button"
              className="link"
              onClick={() => retry.mutate()}
              disabled={retry.isPending}
            >
              {retried || retry.isError
                ? t("recycleBins.banner.tryAgain")
                : t("recycleBins.banner.turnBackOn")}
            </button>
          </span>
        </div>
      ))}
    </>
  );
}
