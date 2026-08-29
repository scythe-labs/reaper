// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Lists. Every protection list, whether each is still protecting anything, and
// the controls to add, edit and check them.
//
// **Settings defines what a list is and where it comes from. Policy defines what it does.**
// Nothing is configured in two places: what a list does is a keep rule on Policy naming it,
// and each row here says how the policies are using it right now (`policy_use`), because a
// defined list no rule names is a list that protects nothing.
//
// **One row per definition, never one per stored list.** Reaper stores a tag list per *arr
// instance, so two Radarrs and two Sonarrs are four stored rows for the single thing the
// operator did. They collapse into one row carrying the family's worst state, which names only
// the servers that need attention. The join is `ProtectionList.list_id`, derived on the server
// beside the slug spellings. This component never parses a slug.
//
// The verdict is the server's (`lists.ListHealth`), not this component's. Two surfaces
// deciding for themselves what `last_error` plus `last_synced_at` means is how the screen and
// the scan could end up telling the operator different stories about one failed check. What
// is decided here is only the sentence.
//
// The row grammar is the Jobs tab's (`.jobrow`), not a second settings-row shape. Both pages
// answer the same question, is this thing working and when did it last run, so a reader
// moving between them should not have to learn two layouts.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";

import { announce } from "../announce";
import { api, type ListConfig, type ProtectionList } from "../api";
import { useBackGuard } from "../backnav";
import { describeError } from "../errors";
import { since } from "../format";
import i18next from "../i18n";
import { composeIn } from "../why";
import { ListModal } from "./ListModal";
import { Notice } from "./Notice";
import { rescanHeading, rescanQueuedLead } from "./PolicySimulator";

function titles(n: number): string {
  return i18next.t("lists.titleCount", { n });
}

type MediaType = "movie" | "tv";

/** The words for a set of media types, movies before shows so the sentence reads the same
 *  order every time: "movies", "shows", or "movies and shows". This says "movies"/"shows",
 *  never "movie"/"tv": the operator sees their libraries, not the rating-key spaces
 *  underneath. */
function mediaWords(types: MediaType[]): string {
  const hasMovie = types.includes("movie");
  const hasTv = types.includes("tv");
  const which = hasMovie && hasTv ? "both" : hasMovie ? "movie" : hasTv ? "tv" : "none";
  return i18next.t("lists.mediaWords", { which });
}

/** What a list protects, and what is still exposed on it: the two sets a mixed list is read
 *  against. A keep rule protects a media type, and a list holds whichever types its members
 *  do, so a rule naming a mixed list on one policy leaves the other side deletable.
 *  `protectedTypes` are the types a rule names and the list is confirmed to hold.
 *  `exposed` are types on the list no rule covers. Both are empty until a sync fills
 *  `spanned`, so an unchecked list claims neither. */
type Coverage = { protectedTypes: MediaType[]; exposed: MediaType[]; hasSplitPill: boolean };

function coverageOf(
  policyUse: { media_type: MediaType }[],
  spanned: Set<MediaType>,
  hasSplitPill: boolean,
): Coverage {
  const covered = new Set(policyUse.map((u) => u.media_type));
  const order: MediaType[] = ["movie", "tv"];
  return {
    protectedTypes: order.filter((t) => spanned.has(t) && covered.has(t)),
    exposed: order.filter((t) => spanned.has(t) && !covered.has(t)),
    hasSplitPill,
  };
}

/** The count line for a list a rule covers only part of: what it keeps, then what is still
 *  deletable. `protectedTypes` is empty when a rule names a side the list does not hold, so
 *  the sentence leads straight with the exposed side. */
function partialDetail({ protectedTypes, exposed }: Coverage): string {
  const kept = protectedTypes.length
    ? i18next.t("lists.partial.kept", { words: mediaWords(protectedTypes) })
    : "";
  const still = mediaWords(exposed);
  const capitalized = still.charAt(0).toUpperCase() + still.slice(1);
  return i18next.t("lists.partial.detail", { kept, still: capitalized });
}

/** The chip's hover title: what the list is protecting right now. Undefined when a rule names
 *  it but Reaper has not confirmed a covered type is on it, so the chip carries no claim it
 *  cannot stand behind. */
function protectingTitle({ protectedTypes, exposed }: Coverage): string | undefined {
  if (!protectedTypes.length) return undefined;
  return i18next.t("lists.protectingTitle", {
    only: exposed.length ? "true" : "false",
    words: mediaWords(protectedTypes),
  });
}

type Tone = "ok" | "warn" | "bad" | "idle";
type State = ProtectionList["state"];

/** This screen's four states in the app's one chip family (`.status-chip`,
 *  23-queue-chips.css), rather than a `.list-state` family of its own re-declaring the same
 *  token pairs: a color meaning "kept" has to move everywhere at once. Worn with
 *  `.status-chip-wrap`, the family's non-truncating variant, because these labels are not in
 *  a fixed column here. */
const STATE_CHIP: Record<Tone, string> = {
  ok: "status-kept",
  warn: "status-warn",
  bad: "status-pressure",
  idle: "status-quiet",
};

/** Worst first. A group wears its worst member's state, because a family reported by its best
 *  member is a family that says "Working" while one server's tag list protects nothing. */
const SEVERITY: State[] = ["failing", "never_checked", "stale", "working"];

function worst(rows: ProtectionList[]): State {
  return SEVERITY.find((s) => rows.some((r) => r.state === s)) ?? "working";
}

/** The chip and the count line under it, for one row or one collapsed family.
 *
 *  The row says one true thing: whether a keep rule uses the list (the chip), and how many
 *  titles are on it (the line). The strength of the rule is a separate fact that belongs on
 *  Policy, read there: "Keeps every title on it" would be wrong the moment the rule is a
 *  lean. The count reads "on it" when the last check just succeeded, and "cached" when the
 *  figure is the last good one a failed, stale, or not-yet-run check left behind. Neither
 *  line makes a "protecting" claim, which the rule's strength could make false either way. */
function describe(
  state: State,
  items: number,
  used: boolean = true,
  coverage?: Coverage,
): { label: string; tone: Tone; detail: string } {
  const onIt = i18next.t("lists.describe.onIt", { titles: titles(items) });
  const cached = i18next.t("lists.describe.cached", { titles: titles(items) });

  // A list no keep rule names protects nothing, whatever its sync says, so the chip, the
  // screen's answer to "is this keeping my titles", reads "Not in use", never the green "In
  // use". The empty guard below is the same fail-toward-keeping check from the other side:
  // full, checked, and keeping nothing. Orphan rows pass no `used` and default to true, since
  // they carry no definition to hold a rule, and may still protect through a legacy one.
  if (!used) {
    return {
      label: i18next.t("lists.describe.notInUseLabel"),
      tone: "idle",
      detail: items > 0 ? onIt : i18next.t("lists.describe.nothingOnItDetail"),
    };
  }
  // Empty and checked is never green. A "Never Reap" collection sitting green at 0 titles
  // would tell an operator covered by nothing that they were covered, indistinguishable from
  // Reaper reading the wrong library or a list whose entries it cannot identify.
  if (items === 0 && (state === "working" || state === "stale")) {
    return {
      label: i18next.t("lists.describe.nothingOnItLabel"),
      tone: "idle",
      detail: i18next.t("lists.describe.checkTheOneYouMeant"),
    };
  }
  switch (state) {
    case "working":
      // A keep rule covers one side of a mixed list and leaves the other still deletable.
      // The chip stays "In use", since the list is in use, and the partial cover is carried
      // two ways: the split pill dims its exposed half, and this line names it. The chip goes
      // amber only where there is no split pill to dim (a flat IMDb/Plex badge), so that lane
      // still has an at-a-glance warning.
      if (coverage && coverage.exposed.length > 0) {
        return {
          label: i18next.t("lists.describe.inUseLabel"),
          tone: coverage.hasSplitPill ? "ok" : "warn",
          detail: partialDetail(coverage),
        };
      }
      return { label: i18next.t("lists.describe.inUseLabel"), tone: "ok", detail: onIt };
    case "stale":
      return { label: i18next.t("lists.describe.outOfDateLabel"), tone: "warn", detail: cached };
    case "failing":
      return {
        label: i18next.t("lists.describe.notWorkingLabel"),
        tone: "bad",
        detail: items > 0 ? cached : i18next.t("lists.describe.nothingCached"),
      };
    case "never_checked":
      return {
        label: i18next.t("lists.describe.notCheckedLabel"),
        tone: "idle",
        detail: items > 0 ? cached : i18next.t("lists.describe.runsNextScan"),
      };
  }
}

/** The kind badge beside the name: which family the list reads from, in each service's own
 *  colors. A tag list reads Sonarr and Radarr at once, so its pill is half of each.
 *
 *  When `coverage` is given, each half is bright only where a keep rule covers that side and
 *  the list is confirmed to hold it. The exposed half fades. Sonarr is shows, Radarr is
 *  movies. The dim is not the only carrier of that fact: the sr-only name states the exposed
 *  side, and the count line names it in a sentence, because dim alone is a color cue a reader
 *  never gets. */
function kindBadge(source: ListConfig["source"], coverage?: Coverage): ReactNode {
  if (source === "arr_tag") {
    // One span per half, so each name is centered in its own color instead of riding a
    // gradient stop that falls near, but not on, the space between them.
    //
    // The halves are hidden from a reader and the name is said once, in text. The two spans
    // are flex items, and whether the whitespace between two flex items reaches the
    // accessibility tree is not something to bet a name on: a whitespace-only anonymous item
    // generates no box at all. Said outright, it cannot come out as one invented word, and
    // the claim does not rest on layout behavior no test here can observe.
    const bright = (t: MediaType) => !coverage || coverage.protectedTypes.includes(t);
    // Only when one side is covered and the other is not: a list nothing covers already reads
    // "Not in use" on the chip, so the badge saying "not kept" too would be the same fact twice.
    const partial =
      coverage !== undefined && coverage.protectedTypes.length > 0 && coverage.exposed.length > 0;
    return (
      <span className="kind-badge kind-arr">
        <span aria-hidden="true" className={bright("tv") ? undefined : "dim"}>
          {i18next.t("common.brand.sonarr")}
        </span>
        <span aria-hidden="true" className={bright("movie") ? undefined : "dim"}>
          {i18next.t("common.brand.radarr")}
        </span>
        <span className="sr-only">
          {i18next.t("lists.kindBadge.srOnly", {
            partial: partial ? "true" : "false",
            exposedWords: partial ? mediaWords(coverage.exposed) : "",
          })}
        </span>
      </span>
    );
  }
  if (source === "imdb") {
    return <span className="kind-badge kind-imdb">{i18next.t("common.brand.imdb")}</span>;
  }
  return <span className="kind-badge kind-plex">{i18next.t("common.brand.plex")}</span>;
}

/** One rendered row: the Jobs tab's shape, with the state chip on the title line and real
 *  actions where a job's buttons sit. Check now is the rightmost action on every row. */
function ListRow({
  title,
  badge,
  detail,
  label,
  tone,
  chipTitle,
  meta,
  error,
  checking,
  onCheck,
  onEdit,
  checkError,
  children,
}: {
  title: string;
  badge?: ReactNode;
  detail: ReactNode;
  label: string;
  tone: Tone;
  /** The chip's hover title: what the list is protecting, for a list a rule covers. Absent
   *  where there is nothing to say (not in use, never checked). */
  chipTitle?: string | undefined;
  meta: string | null;
  error?: string | null;
  checking: boolean;
  onCheck: () => void;
  /** Absent for a row stored before its definition existed, which has nothing to edit. */
  onEdit?: (() => void) | undefined;
  /** Why the last check pressed on this row failed. Rendered here rather than in a page-level
   *  slot, because it is about this list and the button that retries it is on this row. */
  checkError?: string | null | undefined;
  /** The row's own extras: tag pills, the per-server fold-out, the Configure in Policy action. */
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const checkingState = checking ? "true" : "false";
  return (
    <div className="jobrow">
      <div className="jobrow-main">
        {/* The name arrives from Plex or an *arr, or from the operator's own keyboard, so it
            wraps rather than running through the box holding it. */}
        <div className="list-head">
          <span className="jobrow-title list-name">{title}</span>
          {badge}
          <span className={`status-chip status-chip-wrap ${STATE_CHIP[tone]}`} title={chipTitle}>
            {label}
          </span>
        </div>
        <div className="jobrow-desc">{detail}</div>
        {children}
        {meta !== null && <div className="jobrow-sched">{meta}</div>}
        {/* The service's own words, which name the thing to go and fix: a library not called
            what Reaper asked for, a tag that was renamed. Also from outside the app. */}
        {error != null && <div className="jobrow-desc list-error">{error}</div>}
        {checkError != null && (
          <Notice tone="error" inline>
            {t("lists.checkFailed", { error: checkError })}
          </Notice>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit">
          {onEdit && (
            <button
              className="ghost"
              aria-label={t("lists.row.editAriaLabel", { title })}
              onClick={onEdit}
            >
              {t("common.edit")}
            </button>
          )}
        </span>
        <span className="slot-act">
          <button
            className="primary"
            // The state is in the accessible name and the visible words lead, so voice
            // control still reaches the button while it is working (the shape JobRow uses).
            aria-label={t("lists.row.checkAriaLabel", { checking: checkingState, title })}
            onClick={onCheck}
            disabled={checking}
          >
            {t("lists.row.checkButton", { checking: checkingState })}
          </button>
        </span>
      </div>
    </div>
  );
}

/** The tag pills and the per-server fold-out, for a tag list's row. Counts are summed across
 *  the family's rows, one per *arr instance, and a tag no row has counted yet renders as the
 *  bare pill, never as zero (the counts are unknown, not empty). */
function TagCounts({ definition, mine }: { definition: ListConfig; mine: ProtectionList[] }) {
  const { t } = useTranslation();
  const tags = definition.config.tags ?? [];
  if (tags.length === 0) return null;
  const counted = mine.filter(
    (r): r is ProtectionList & { tags: Record<string, number> } => r.tags !== null,
  );
  const total = (tag: string): number | null => {
    const holders = counted.filter((r) => r.tags[tag] !== undefined);
    if (holders.length === 0) return null;
    return holders.reduce((n, r) => n + (r.tags[tag] ?? 0), 0);
  };
  // Only servers with something to say about THESE tags: a stats body written before the
  // counts existed, or for tags since renamed, would otherwise render a server name beside
  // an empty count column.
  const perServer = counted.filter(
    (r) => r.server !== null && tags.some((tag) => r.tags[tag] !== undefined),
  );
  // The matrix's margins sum only the servers it SHOWS, so its rows and columns always add up.
  // A title whose server Reaper never learned is in the pill totals above but has no column to
  // sit in, so the pill and the matrix Total can differ by exactly those unplaced titles.
  const colTotal = (r: (typeof perServer)[number]) =>
    tags.reduce((n, tag) => n + (r.tags[tag] ?? 0), 0);
  const rowTotal = (tag: string) => perServer.reduce((n, r) => n + (r.tags[tag] ?? 0), 0);
  const grandTotal = perServer.reduce((n, r) => n + colTotal(r), 0);
  return (
    <>
      <div className="tag-pills">
        {tags.map((tag) => {
          const n = total(tag);
          return (
            // One text run, not two flex items. As flex children the tag and its count had
            // nothing between them at all, so the pill read and COPIED as "reaper-keep412".
            // A literal space rather than a gap, so the separation is in the text itself and
            // does not depend on how whitespace between flex items is treated.
            <span key={tag} className="tag-pill">
              {tag} {n !== null && <b>{n.toLocaleString()}</b>}
            </span>
          );
        })}
      </div>
      {perServer.length > 0 && (
        <details className="per-server">
          <summary>{t("lists.tagCounts.countsByServer")}</summary>
          {/* Tags down the side, servers across the top, counts at the intersections: a tag
              reads across its row and a server down its column, and the figures line up. A
              single comma-joined line per server would turn to mush as tags and servers
              multiply. The box scrolls sideways when it outgrows the screen (many servers),
              and the Tag column stays pinned. The table sizes to its content rather than
              being forced to the box width, which on a phone would squeeze a tag name to one
              glyph per line. The cells do not wrap. They are reached by scrolling instead,
              which the outside-text guard records as a deliberate exception. */}
          {/* tabIndex so a keyboard operator can focus the box and scroll it: no cell is
              focusable, so without it the matrix is unreadable past its first screenful on a
              narrow pane (WCAG 2.1.1). */}
          <div className="matrix-scroll" tabIndex={0}>
            <table className="tag-matrix">
              <thead>
                <tr>
                  <th scope="col" className="corner">
                    {t("lists.tagCounts.tagColumn")}
                  </th>
                  {perServer.map((r) => (
                    <th scope="col" key={r.slug}>
                      {r.server}
                    </th>
                  ))}
                  <th scope="col" className="tot">
                    {t("lists.tagCounts.total")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {tags.map((tag) => (
                  <tr key={tag}>
                    <th scope="row">{tag}</th>
                    {perServer.map((r) => {
                      // A tag the instance does not carry is a dash, distinct from a real zero;
                      // both read faint, so a populated cell is what the eye lands on.
                      const n = r.tags[tag];
                      return (
                        <td key={r.slug} className={n ? undefined : "empty"}>
                          {/* A "no value" placeholder, so a screen reader hears an empty cell
                              rather than voicing a dash. A real zero is spoken. */}
                          {n === undefined ? <span aria-hidden="true">—</span> : n.toLocaleString()}
                        </td>
                      );
                    })}
                    <td className="tot">{rowTotal(tag).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th scope="row">{t("lists.tagCounts.total")}</th>
                  {/* Plain cells: the footer's own top border sets the Total ROW off, so a
                      per-server total needs no border of its own. Only the grand total carries
                      `.tot`, which continues the Total COLUMN's single divider into the footer
                      and lines up under the body's. Giving every footer cell `.tot` drew a
                      stray vertical between each one. */}
                  {perServer.map((r) => (
                    <td key={r.slug}>{colTotal(r).toLocaleString()}</td>
                  ))}
                  <td className="tot">{grandTotal.toLocaleString()}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </details>
      )}
    </>
  );
}

/** Which rows a check is running for. A number is one definition. `"all"` is the whole-pass
 *  target, which only an orphan row uses now, since a stored row no definition owns has no
 *  id to check by, so checking everything is the only pass that can reach it. This screen has
 *  no "Check all now" button of its own: the nightly job checks every list and has its own
 *  Run now on Settings, Jobs. */
type CheckTarget = number | "all";

export function ListsPanel({
  onGoToPolicy,
}: {
  /** Jump to the Policy screen's keep-rules section, where a list's rules live. Optional the
   *  way `SafetyBanner`'s jump is: without a navigator the sentences render and the link does
   *  not. */
  onGoToPolicy?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const health = useQuery({ queryKey: ["lists"], queryFn: api.lists });
  const definitions = useQuery({ queryKey: ["lists-configured"], queryFn: api.listConfigs });

  // `null` is closed, `{ list: null }` is Add, `{ list }` is Edit. One piece of state rather
  // than an open flag beside a subject, so the two cannot disagree about what is on screen.
  const [modal, setModal] = useState<{ list: ListConfig | null } | null>(null);
  // The modal decides when it may be dismissed and mirrors that whole answer here, the
  // arrangement the service and schedule editors use. Without this, Back would fall through
  // to the Settings section frame: the panel would navigate, this component would unmount,
  // and an in-flight save's refusal would go with it while the operator walked away believing
  // the list saved.
  const blockCloseRef = useRef(false);
  useBackGuard(
    modal !== null,
    () => setModal(null),
    () => !blockCloseRef.current,
  );

  // Editing or removing a list that a keep rule names changes which titles are protected,
  // and the queue is showing fates scored under the lists as they were. Nothing here
  // re-scores them: a check refreshes membership, which is a different thing, so the queue
  // would keep its stale fates with no stale notice, and an approved plan would meet the
  // executor's list interlock at the far end. `PolicyEditor` starts a scan on save for
  // exactly this class of change, and a list is the half of the policy that moved out of the
  // policy body.
  //
  // The modal decides whether a fate moved (`onChanged`'s `rescore`), and this fires only
  // then, so adding a list nothing uses, which writes no rule and can change no fate, does
  // not scan the whole library for nothing. It lives on the panel rather than in the modal
  // because the modal is unmounting: a mutation started on the way out loses the surface that
  // would report it. This is idempotent server-side, so a scan already running is simply
  // followed.
  const startScan = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => {
      queryClient.setQueryData(["scanStatus"], started);
      // Spoken, because starting a scan is otherwise invisible from this screen: the
      // progress it drives is on another tab. Same two sentences `PolicyEditor` says, from
      // the panel that owns them, branching for the same reason. A scan already running is
      // scoring the lists as they were before this edit.
      announce(started.followup_queued ? rescanQueuedLead() : `${rescanHeading()}.`);
    },
  });

  const check = useMutation({
    mutationFn: (target: CheckTarget) => api.syncLists(target === "all" ? {} : { list_id: target }),
    // The row's chip is the result, and it is only true once the refetch has landed.
    // Awaiting the invalidation inside the mutation keeps `isPending` true until then, so the
    // button says "Checking…" for exactly as long as the answer on screen is the old one.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["lists"] }),
  });
  const running = check.isPending ? check.variables : null;
  const failedTarget = check.isError ? check.variables : null;
  /** Whether `target`'s button is the one to show as busy. "Check all now" really is
   *  checking every row, so every row says so and none of them can be pressed again while
   *  it runs. Found by driving it: the footer and the rows disagreed, and a row still
   *  offering "Check now" during the pass it was already part of would invite a second check
   *  that could race the first. Reachable from an orphan row's own button now that the
   *  footer's is gone. */
  const busy = (target: CheckTarget) => running === target || running === "all";

  // Both reads, explicitly. This panel's contract is "always visible", so a failure renders
  // a fallback saying what is unknown rather than an empty screen implying there is nothing
  // to protect.
  if (health.isPending || definitions.isPending) {
    return (
      <div className="panel">
        <h2>{t("lists.heading")}</h2>
        <p className="muted">{t("lists.loading")}</p>
      </div>
    );
  }
  if (health.isError || definitions.isError) {
    const fetching = health.isFetching || definitions.isFetching;
    const retry = (
      <button
        className="ghost sm"
        disabled={fetching}
        onClick={() => {
          void health.refetch();
          void definitions.refetch();
        }}
      />
    );
    return (
      <div className="panel">
        <h2>{t("lists.heading")}</h2>
        {/* An alert, not `standing`: these are fetched when the tab is opened rather than
            polled, so it mounts once with something the operator has not been told yet. */}
        <Notice tone="error">
          {/* Says it is retrying while it is. The query's status stays `error` across a
              refetch, and only `isFetching` moves, so the press would change nothing on
              screen until it settled, which reads as a dead button. The control reports the
              state it is in, the same as the check buttons on the rows. */}
          {fetching ? (
            <Trans i18nKey="lists.loadErrorRetrying" components={{ btn: retry }} />
          ) : (
            <Trans i18nKey="lists.loadError" components={{ btn: retry }} />
          )}
        </Notice>
      </div>
    );
  }

  const rows = health.data;
  /** Every health row synced for one definition. Several for a tag list: one per *arr. */
  const forList = (id: number) => rows.filter((r) => r.list_id === id);

  // Anything stored before a definition existed to own it. The server re-homes such rows
  // onto their definitions before answering (`lists.adopt_legacy`), so what reaches here is
  // only what no definition could safely claim. The next successful check retires it. It is
  // still protecting until then, so it is rendered rather than hidden. A row that holds
  // titles and is invisible here is the failure this screen exists to fix.
  const orphans = rows.filter((r) => r.list_id === null);

  return (
    <div className="panel">
      <h2>{t("lists.heading")}</h2>
      <p className="blurb">{t("lists.blurb")}</p>

      <div className="set-rows">
        {definitions.data.map((definition) => {
          const mine = forList(definition.id);
          const state = worst(mine);
          const items = mine.reduce((n, r) => n + r.item_count, 0);
          // A list no keep rule names protects nothing, so its chip reads "Not in use", never
          // green "In use", however its sync went.
          const used = definition.policy_use.length > 0;
          // The media types the list's members actually span, unioned across the family's
          // rows. A tag list is one row per *arr, so movies come off the Radarr rows and
          // shows off the Sonarr ones. This is compared against the types a keep rule names,
          // to tell a fully-covered list from one covered on a single side.
          const spanned = new Set<MediaType>(mine.flatMap((r) => r.media_types));
          const coverage = coverageOf(
            definition.policy_use,
            spanned,
            definition.source === "arr_tag",
          );
          const shown =
            mine.length === 0
              ? describe("never_checked", 0, used)
              : describe(state, items, used, coverage);
          // What the chip says on hover, only for a list a rule actually covers.
          const chipTitle = used ? protectingTitle(coverage) : undefined;
          // A row with nothing stored is the one whose count a running check contradicts:
          // "Runs with your next scan" beside a button that says "Checking…". That pair is now
          // what every operator sees for the length of the check a save starts, so the row says
          // what is happening instead. Every other state describes the LAST check and stays
          // true while the next one runs.
          const count =
            busy(definition.id) && mine.length === 0 ? t("lists.checkingItNow") : shown.detail;
          // An in-use list links to its rule to adjust it, and a not-in-use one offers the
          // setup action below. Both go to Policy: you change the policy there, never "on" it
          // from here.
          const detail =
            used && onGoToPolicy ? (
              <>
                {count}{" "}
                <button className="link policy-change" onClick={onGoToPolicy}>
                  {t("lists.changePolicy")}
                </button>
              </>
            ) : (
              count
            );
          // Sorted ascending and read at [0], so a family reports its OLDEST check: the row
          // is a claim about the whole family, and the newest member's stamp would say the
          // protection is fresher than its weakest part. Same direction as `worst` above,
          // and stated because reading the stalest of a set looks like an off-by-one.
          const checked = mine
            .map((r) => r.last_checked_at)
            .filter((v): v is string => v !== null)
            .sort();
          const errors = [...new Set(mine.map((r) => r.error).filter(Boolean))];
          const across =
            definition.source === "arr_tag" && mine.length > 1
              ? t("lists.meta.across", { n: mine.length })
              : "";
          return (
            <ListRow
              key={definition.id}
              title={definition.name}
              badge={kindBadge(definition.source, coverage)}
              detail={detail}
              label={shown.label}
              tone={shown.tone}
              chipTitle={chipTitle}
              meta={
                (checked.length ? t("lists.meta.lastChecked", { when: since(checked[0]!) }) : "") +
                across +
                sourceHint(definition)
              }
              error={errors.length ? errors.join(" ") : null}
              checking={busy(definition.id)}
              onCheck={() => check.mutate(definition.id)}
              onEdit={() => setModal({ list: definition })}
              checkError={
                failedTarget === definition.id && check.error ? describeError(check.error) : null
              }
            >
              {definition.source === "arr_tag" && <TagCounts definition={definition} mine={mine} />}
              {!used && onGoToPolicy && (
                <button className="link policy-configure" onClick={onGoToPolicy}>
                  {t("lists.configureInPolicy")}
                </button>
              )}
            </ListRow>
          );
        })}

        {orphans.map((row) => {
          const shown = describe(row.state, row.item_count);
          return (
            <ListRow
              key={row.slug}
              title={row.name}
              detail={shown.detail}
              label={shown.label}
              tone={shown.tone}
              meta={
                (row.last_checked_at
                  ? t("lists.meta.lastChecked", { when: since(row.last_checked_at) })
                  : "") + t("lists.orphanNote")
              }
              error={row.error}
              checking={busy("all")}
              onCheck={() => check.mutate("all")}
            />
          );
        })}
      </div>

      {/* The sink for a failed check of everything, whether it was started here or from an
          orphan row, whose button drives the "all" target. Every row's own sink compares
          `failedTarget` against a definition id, which is a number and can never equal "all",
          so without this, the whole-pass failure would have nowhere to land: the button
          would go back to rest, every row would keep its stale chip, and nothing would say
          the check had not run. */}
      {failedTarget === "all" && (
        <Notice tone="error">
          {t("lists.checkFailed", { error: check.error ? describeError(check.error) : "" })}
        </Notice>
      )}

      {/* Only while the check it came from is the last thing that happened. `check.data`
          holds the previous result until the next mutation replaces it, so a Plex warning
          from one check could stay on screen through an unrelated Edit or Add, reading as
          though it were about those. A check in flight has not answered yet, so it says
          nothing. */}
      {!check.isPending && check.data?.plex_error_reason && (
        <Notice tone="warn">{composeIn("lists", check.data.plex_error_reason)}</Notice>
      )}

      {/* No "Check all now" here. Checking everything is the upkeep job's whole purpose: it
          runs nightly and has its own Run now on Settings, Jobs. A second button meaning
          "refresh all of them" here would be the same job offered twice. Per-row Check now
          stays: it is the one thing the job cannot do, which is check the single list you
          just edited. */}
      <div className="list-foot">
        <span className="flex-spacer" />
        <button className="primary" onClick={() => setModal({ list: null })}>
          {t("lists.addList")}
        </button>
      </div>

      {modal && (
        <ListModal
          editing={modal.list}
          onClose={() => setModal(null)}
          blockCloseRef={blockCloseRef}
          // A saved list is checked on the spot, through the same mutation the row's own
          // button drives: a list is protecting nothing until something reads it, and an
          // operator who has just said what the list is should not have to say "now go and
          // read it" as a second step. The row shows it as busy and carries whatever comes
          // back, a count or the source's own refusal, which is the answer to the question
          // they were really asking when they saved.
          onSaved={(list) => check.mutate(list.id)}
          // Only when the change moved what a keep rule protects: an edit or a remove of a
          // list a rule names re-judges the queue, and an add, which writes no rule, does
          // not.
          onChanged={(rescore) => rescore && startScan.mutate()}
        />
      )}
    </div>
  );
}

/** Where a row's list comes from, in one clause, so a reader can tell two lists apart when
 *  their names do not. The operator named the list; this says what they pointed it at. */
function sourceHint(definition: ListConfig): string {
  if (definition.source === "plex_collection") {
    const { library, collection } = definition.config;
    return library && collection
      ? i18next.t("lists.sourceHint.plexCollectionNamed", { collection, library })
      : i18next.t("lists.sourceHint.plexCollectionGeneric");
  }
  if (definition.source === "plex_watchlist") {
    return i18next.t("lists.plexWatchlistDescription");
  }
  if (definition.source === "arr_tag") {
    // The "Counts by server" fold-out already names which Sonarrs and Radarrs were read, so the
    // meta line only carries what that does not: whether a title needs every tag or any one.
    return definition.config.match === "all" ? i18next.t("lists.sourceHint.tagsNeedAll") : "";
  }
  return definition.config.preset
    ? i18next.t("lists.sourceHint.selfUpdating")
    : i18next.t("lists.sourceHint.imdbList", { id: definition.config.list_id ?? "" });
}
