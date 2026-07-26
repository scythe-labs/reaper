// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The live log viewer, and the logging level that feeds it.
//
// Every removal decision is answerable from here, so the window keeps the newest lines
// even while you are somewhere else in Settings: the accumulated lines live at module
// scope (see _logStore) and the panel seeds from them on mount.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type LogLine } from "../api";
import { count } from "../format";
import { Switch } from "./Switch";

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
 *  The search used to lowercase `text` and `level` for every one of up to 2000 lines on every
 *  render -- and this panel re-renders on each 2s poll and each keystroke (P-6). Lowercasing
 *  is done once, when the line arrives and never changes again. */
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

  const setLevel = useMutation({
    mutationFn: api.setLogLevel,
    onSuccess: (page) => setRecordLevel(page.level),
  });

  const download = useMutation({ mutationFn: api.downloadLogs });

  return (
    <div className="panel">
      <h2>Logs</h2>
      <p className="muted">
        What Reaper is doing right now, and the trail of what it did. Every removal decision is
        answerable from here. The newest {count(2000)} lines are kept.
      </p>

      <div className="logbar">
        <input
          type="search"
          className="log-search"
          placeholder="Search the log…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search the log"
        />
        <select
          value={minLevel}
          onChange={(e) => setMinLevel(e.target.value)}
          aria-label="Only show this level and up"
        >
          {/* No "Debug and up": debug is the lowest level there is, so it would keep
              every line, which is what "All levels" already does. Each option names a
              floor that filters something out. */}
          <option value="all">All levels</option>
          <option value="INFO">Info and up</option>
          <option value="WARNING">Warnings and up</option>
          <option value="ERROR">Errors only</option>
        </select>
        {/* Both of these are on/off states, so both are the product's one on/off control
            rather than a pair of pressed buttons that each said "on" a different way. */}
        <label className="toggle">
          <Switch checked={live} onChange={setLive} ariaLabel="Follow new lines" />
          <span>Follow new lines</span>
        </label>
        <label className="toggle">
          <Switch
            checked={wrap}
            onChange={(next) => {
              setWrap(next);
              _logStore.wrap = next;
            }}
            ariaLabel="Wrap long lines"
          />
          <span>Wrap long lines</span>
        </label>
        <span className="muted log-count">
          {visible.length === lines.length
            ? `${count(lines.length)} ${lines.length === 1 ? "line" : "lines"}`
            : `${count(visible.length)} of ${count(lines.length)} lines`}
        </span>
      </div>

      {logs.isPending && lines.length === 0 ? (
        <p className="muted">Loading the log…</p>
      ) : logs.isError && lines.length === 0 ? (
        <p className="notice notice-error">
          Couldn't load the log.{" "}
          <button className="ghost sm" onClick={() => void logs.refetch()}>
            Try again
          </button>
        </p>
      ) : (
        <div
          className={wrap ? "log-console log-wrap" : "log-console"}
          ref={consoleRef}
          role="log"
          aria-label="Application log"
        >
          {visible.length === 0 ? (
            <p className="muted log-empty">
              {lines.length === 0
                ? "Nothing yet. New lines appear here as Reaper works."
                : "Nothing matches your search."}
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
        <p className="notice notice-error">
          {live ? (
            "Couldn't load new lines. Reaper is trying again."
          ) : (
            <>
              Couldn't load new lines, and updates are paused.{" "}
              <button className="ghost sm" onClick={() => void logs.refetch()}>
                Try again
              </button>
            </>
          )}
        </p>
      )}

      <div className="set-group log-level-group">
        <h3>Logging</h3>
        <div className="set-rows">
          <div className="set-row">
            <span className="set-label">Logging level</span>
            <p className="help">
              How much Reaper writes, both here and in the container output. Info is the everyday
              setting. Debug is chatty and best while chasing a problem. Takes effect immediately,
              no restart.
            </p>
            <div className="set-control">
              <select
                value={recordLevel ?? "INFO"}
                disabled={setLevel.isPending || recordLevel === null}
                aria-label="Logging level"
                onChange={(e) => setLevel.mutate(e.target.value)}
              >
                <option value="DEBUG">Debug</option>
                <option value="INFO">Info</option>
                <option value="WARNING">Warning</option>
              </select>
            </div>
          </div>
          <div className="set-row">
            <span className="set-label">Log files</span>
            <p className="help">
              Save the whole log to your computer, handy for a bug report.
              {logs.data
                ? ` Reaper keeps the newest ${count(logs.data.files_kept)} files on the server, a fuller trail than the window above.`
                : " Reaper keeps a fuller trail on the server than the window above."}
            </p>
            <div className="set-control">
              <button
                className="primary"
                onClick={() => download.mutate()}
                disabled={download.isPending}
              >
                {download.isPending ? "Preparing…" : "Download logs"}
              </button>
            </div>
          </div>
        </div>
        {setLevel.error && <p className="notice notice-error">{setLevel.error.message}</p>}
        {download.error && (
          <p className="notice notice-error">The download didn't start: {download.error.message}</p>
        )}
      </div>
    </div>
  );
}
