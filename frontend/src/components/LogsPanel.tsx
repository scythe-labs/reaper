// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The live log viewer, and the logging level that feeds it.
//
// Every removal decision is answerable from here, so the window keeps the newest lines
// even while you are somewhere else in Settings: the accumulated lines live at module
// scope (see _logStore) and the panel seeds from them on mount.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { announce, useSlowWait } from "../announce";
import { api, type LogLine } from "../api";
import { describeError } from "../errors";
import { Switch } from "./Switch";
import { Notice } from "./Notice";
import { SetRow } from "./SetRow";

const LEVEL_RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

function levelClass(level: string): string {
  const upper = level.toUpperCase();
  if (upper === "DEBUG") return "log-lv debug";
  if (upper === "WARNING") return "log-lv warning";
  if (upper === "ERROR" || upper === "CRITICAL") return "log-lv error";
  return "log-lv info";
}

function logTime(ts: string): string {
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString([], { hour12: false });
}

/** One kept line, with the text the search box actually matches against folded in once.
 *
 *  Lowercasing `text` and `level` for every one of up to 2000 lines on every render would be
 *  wasted work, since this panel re-renders on each 2s poll and each keystroke. Lowercasing
 *  is done once instead, when the line arrives, and never changes again. */
type KeptLine = LogLine & { haystack: string };

const keep = (line: LogLine): KeptLine => ({
  ...line,
  haystack: `${line.text} ${line.level}`.toLowerCase(),
});

// The accumulated log window, kept at module scope so it survives navigating away from the
// Logs tab and back. LogsPanel unmounts when you leave the tab; without this its lines would
// reset to empty and the panel would show "Nothing yet" until the next poll. Seeded from
// here on mount, so the last lines are on screen immediately.
const _logStore: { lines: KeptLine[]; cursor: number; wrap: boolean } = {
  lines: [],
  cursor: 0,
  wrap: false,
};

export function LogsPanel() {
  const { t } = useTranslation();
  const [live, setLive] = useState(true);
  const [search, setSearch] = useState("");
  const [minLevel, setMinLevel] = useState("all");
  const [wrap, setWrap] = useState(_logStore.wrap);
  const [lines, setLines] = useState<KeptLine[]>(_logStore.lines);
  const [recordLevel, setRecordLevel] = useState<string | null>(null);
  const cursor = useRef(_logStore.cursor);
  const consoleRef = useRef<HTMLDivElement | null>(null);

  const logs = useQuery({
    queryKey: ["logs"],
    queryFn: () => api.logs(cursor.current),
    refetchInterval: live ? 2000 : false,
  });

  // Fold each page into the accumulated window, mirrored into the module store so the
  // window survives leaving the tab. The seq guard makes this idempotent when React Query
  // hands the same page twice (mount + focus refetch).
  useEffect(() => {
    const page = logs.data;
    if (!page) return;
    cursor.current = page.last_seq;
    _logStore.cursor = page.last_seq;
    setRecordLevel(page.level);
    if (page.lines.length) {
      setLines((prev) => {
        const newest = prev.at(-1)?.seq ?? 0;
        const fresh = page.lines.filter((l) => l.seq > newest).map(keep);
        const next = fresh.length ? [...prev, ...fresh].slice(-2000) : prev;
        _logStore.lines = next;
        return next;
      });
    }
  }, [logs.data]);

  // The console below is the sibling of the same sweep, and it is the one that stays as it
  // is: `role="log"` mounts holding its first batch, so that batch is not announced, and
  // every line after it is. That is the behavior to want. A log region that announced on
  // insertion would read the operator up to 2000 accumulated lines for opening the tab, which
  // is why the sweep speaks the wait here and never the arrival.
  useSlowWait(logs.isPending && lines.length === 0 ? t("logs.loadingWait") : null);

  // Memoized on exactly what the filter reads. Without it this whole pass ran on every
  // render, and the panel re-renders on each 2s poll and each keystroke in the search box.
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const floor = minLevel === "all" ? null : (LEVEL_RANK[minLevel] ?? 0);
    return lines.filter((line) => {
      if (floor !== null && (LEVEL_RANK[line.level] ?? 20) < floor) return false;
      return needle === "" || line.haystack.includes(needle);
    });
  }, [lines, search, minLevel]);

  // Follow the newest line while Live; leave the scroll alone while paused so reading
  // is undisturbed.
  useEffect(() => {
    if (!live) return;
    const el = consoleRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [visible.length, live]);

  // Saves on change, so this panel holds no unsaved edit and reports no draft to the section
  // rail (`dirtyPanels` in Settings.tsx classifies it `false` and says so). Everything else
  // here is a view filter, which is not an edit. Adding a real draft to this panel would make
  // that classification wrong, since nothing here mentions the rail: leaving the section would
  // then throw the draft away with no confirm, and the compiler cannot catch it, since the key
  // already exists. Report it upward the way `SecurityPanel` does.
  const setLevel = useMutation({
    mutationFn: api.setLogLevel,
    onSuccess: (page) => setRecordLevel(page.level),
  });

  const download = useMutation({ mutationFn: api.downloadLogs });

  /** Try again, and say how it went.
   *
   *  The one genuine press in this panel's failure notices, and the one thing they never
   *  said. `refetch()` on an already-errored query leaves `isError` true throughout, so the
   *  notice never unmounts and its text never changes: a retry that failed again is
   *  indistinguishable from a button that does nothing, which is the absence `announce.tsx`
   *  exists for. Both notices share this, in sentences true of either: one fact, one wording.
   *
   *  `refetch` resolves with the result rather than rejecting, so the outcome is read off it. */
  const retry = async () => {
    const result = await logs.refetch();
    announce(result.isError ? t("logs.retryFailure") : t("logs.retrySuccess"));
  };

  return (
    <div className="panel">
      <h2>{t("logs.title")}</h2>
      <p className="muted">{t("logs.description", { limit: 2000 })}</p>

      <div className="logbar">
        <input
          type="search"
          className="log-search"
          placeholder={t("logs.searchPlaceholder")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label={t("logs.searchLabel")}
        />
        <select
          value={minLevel}
          onChange={(e) => setMinLevel(e.target.value)}
          aria-label={t("logs.levelFilterLabel")}
        >
          {/* No "Debug and up": debug is the lowest level there is, so it would keep
              every line, which is what "All levels" already does. Each option names a
              floor that filters something out. */}
          <option value="all">{t("logs.levelFilterAll")}</option>
          <option value="INFO">{t("logs.levelFilterInfo")}</option>
          <option value="WARNING">{t("logs.levelFilterWarning")}</option>
          <option value="ERROR">{t("logs.levelFilterError")}</option>
        </select>
        {/* Both of these are on/off states, so both are the product's one on/off control
            rather than a pair of pressed buttons that each said "on" a different way. */}
        <label className="toggle">
          <Switch checked={live} onChange={setLive} ariaLabel={t("logs.followLabel")} />
          <span>{t("logs.followLabel")}</span>
        </label>
        <label className="toggle">
          <Switch
            checked={wrap}
            onChange={(next) => {
              setWrap(next);
              _logStore.wrap = next;
            }}
            ariaLabel={t("logs.wrapLabel")}
          />
          <span>{t("logs.wrapLabel")}</span>
        </label>
        <span className="muted log-count">
          {visible.length === lines.length
            ? t("logs.lineCount", { n: lines.length })
            : t("logs.lineCountOfTotal", { visible: visible.length, total: lines.length })}
        </span>
      </div>

      {logs.isPending && lines.length === 0 ? (
        <p className="muted">{t("logs.loading")}</p>
      ) : logs.isError && lines.length === 0 ? (
        // `standing`: `["logs"]` polls every 2s while Follow new lines is on, so this mounts and
        // unmounts with the connection rather than with anything pressed. As an alert a flapping
        // connection re-announced byte-identical text over whoever was reading the pane. What the
        // operator does press is Try again, and `retry` is what answers it.
        <Notice tone="error" standing>
          {t("logs.loadError")}{" "}
          <button className="ghost sm" onClick={() => void retry()}>
            {t("logs.retryButton")}
          </button>
        </Notice>
      ) : (
        // The log is rows of <span>, so there is nothing focusable to tab onto and carry the
        // scroll with. A keyboard operator could not move this pane at all (WCAG 2.1.1). It
        // already named itself, which is the worse half of that state: it advertised a
        // destination the Tab order never visited. `tabIndex={0}` makes the pane its own
        // stop. `.docs-content` was the first of these, and the other five went the same way.
        <div
          className={wrap ? "log-console log-wrap" : "log-console"}
          ref={consoleRef}
          tabIndex={0}
          // Kept, unlike the six loading affordances swept elsewhere. This region mounts
          // holding its first batch, so that batch is never announced, and it must not be, or
          // opening the tab would read out the whole accumulated window. What `role="log"`
          // buys is every line after it, which is exactly the part of a live log worth
          // hearing.
          role="log"
          aria-label={t("logs.consoleLabel")}
        >
          {visible.length === 0 ? (
            <p className="muted log-empty">
              {lines.length === 0 ? t("logs.emptyNoLines") : t("logs.emptyNoMatch")}
            </p>
          ) : (
            visible.map((line) => (
              <div key={line.seq} className="log-row">
                <span className="log-t">{logTime(line.ts)}</span>
                <span className={levelClass(line.level)}>{line.level}</span>
                <span className="log-text">{line.text}</span>
              </div>
            ))
          )}
        </div>
      )}
      {/* "Follow new lines" is the only thing scheduling another fetch, so with it off
          nothing is retrying and saying so would be a lie. In that state the operator gets
          the same Try again button the empty-log branch above offers. */}
      {logs.isError && lines.length > 0 && (
        // `standing`, same as its twin above, and with one more reason: its text swaps between
        // these two branches when Follow new lines is toggled, which as an alert made an
        // unrelated switch speak a failure the operator had already been told about.
        <Notice tone="error" standing>
          {live ? (
            t("logs.retryingError")
          ) : (
            <>
              {t("logs.pausedError")}{" "}
              <button className="ghost sm" onClick={() => void retry()}>
                {t("logs.retryButton")}
              </button>
            </>
          )}
        </Notice>
      )}

      <div className="set-group log-level-group">
        <h3>{t("logs.settingsHeading")}</h3>
        <div className="set-rows">
          <SetRow label={t("logs.levelLabel")} help={t("logs.levelHelp")}>
            <select
              value={recordLevel ?? "INFO"}
              disabled={setLevel.isPending || recordLevel === null}
              aria-label={t("logs.levelLabel")}
              onChange={(e) => setLevel.mutate(e.target.value)}
            >
              <option value="DEBUG">{t("logs.levelDebug")}</option>
              <option value="INFO">{t("logs.levelInfo")}</option>
              <option value="WARNING">{t("logs.levelWarning")}</option>
              {/* REAPER_LOG_LEVEL also takes ERROR, which this picker does not offer:
                  hiding warnings from a tool that deletes files serves nobody. Render it
                  while it is the live level anyway, or a box with no matching option shows
                  blank and the picker stops saying what Reaper is recording. Picking any
                  other level stores that one and drops this option. */}
              {recordLevel === "ERROR" && <option value="ERROR">{t("logs.levelError")}</option>}
            </select>
          </SetRow>
          {/* A button, not a box, so it releases the control track (`.set-row-plain`). */}
          <SetRow
            variant="plain"
            label={t("logs.downloadLabel")}
            help={
              <>
                {t("logs.downloadHelpBase")}{" "}
                {logs.data
                  ? t("logs.downloadRetentionKnown", { n: logs.data.files_kept })
                  : t("logs.downloadRetentionUnknown")}
              </>
            }
          >
            <button
              className="primary"
              onClick={() => download.mutate()}
              disabled={download.isPending}
            >
              {download.isPending ? t("common.preparing") : t("logs.downloadButton")}
            </button>
          </SetRow>
        </div>
        {setLevel.error && <Notice tone="error">{describeError(setLevel.error)}</Notice>}
        {download.error && (
          <Notice tone="error">
            {t("logs.downloadError", { message: describeError(download.error) })}
          </Notice>
        )}
      </div>
    </div>
  );
}
