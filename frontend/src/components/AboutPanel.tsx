// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> About: which version is running, what changed since then, and how much
// database space is used.
//
// This panel is read-only, so it holds no draft. `dirtyPanels` in Settings.tsx marks it
// `false`. The update check uses the shared `useUpdateStatus` hook, the same one the masthead
// chip's light reads, so the light and this row always answer from the same read. The pill
// shown here is local to this file, which is why the note further down tells all three
// surfaces apart.
//
// The copy lives in `locales/en/ui.json` under `about.*`.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { api, type ReleaseChange } from "../api";
import Markdown from "react-markdown";
import { useUpdateStatus } from "../updateStatus";
import { bytes } from "../format";
import { ModalShell } from "./ModalShell";
import { StaleReadNotice } from "./StaleReadNotice";
import { Notice } from "./Notice";

export function AboutPanel() {
  const { t } = useTranslation();
  const { data, isPending, isError } = useQuery({ queryKey: ["about"], queryFn: api.about });
  const update = useUpdateStatus();
  const [changesOpen, setChangesOpen] = useState(false);

  return (
    <div className="panel">
      <h2>{t("about.title")}</h2>
      <p className="blurb">{t("about.blurb")}</p>
      {/* standing: the build's channel does not change while the app is running, so this
          notice shows from the first paint. It never reacts to something the operator did. */}
      {update.data?.channel === "dev" && (
        <Notice tone="warn" standing>
          <Trans i18nKey="about.devBuildWarning" components={{ code: <code /> }} />
        </Notice>
      )}
      {isPending && <p className="muted">{t("common.loading")}</p>}
      {/* Two cases, not one. React Query keeps the last good row through a failed refetch and
          sets isError beside it, so treating isError as one case would print "couldn't load
          this page" directly above the page it just drew. The trigger is a remount past
          `staleTime`: leaving About and coming back 30 seconds later while the server is
          unreachable. It is not window focus (`main.tsx` turns that off app-wide; only
          `useSafety` turns it back on for its own query), and not an invalidation, since
          nothing in the app invalidates `["about"]`. */}
      {isError && !data && <Notice tone="error">{t("common.pageLoadError")}</Notice>}
      {isError && data && <StaleReadNotice what={t("about.staleReadWhat")} />}
      {data && (
        <div className="set-rows">
          <dl className="about-kv">
            <dt>{t("about.labels.version")}</dt>
            <dd>
              {t("about.reaperVersion", { version: data.version })}
              {/* Amber, and the same three words on both channels. The operator's question
                  is always whether to go update; the row beneath is where the channels
                  differ. This pill shares the dev banner's amber, not the accent color, so
                  "there is something to do" reads as one signal down the panel instead of
                  two colors for one fact. */}
              {update.data?.update_available && (
                <span className="update-pill">{t("about.updatePill.updateAvailable")}</span>
              )}
            </dd>
            <dt>{t("about.labels.update")}</dt>
            <dd>
              <UpdateCell status={update} onSeeChanges={() => setChangesOpen(true)} />
            </dd>
            <dt>{t("about.labels.license")}</dt>
            <dd>{data.license}</dd>
            <dt>{t("about.labels.dataFolder")}</dt>
            <dd>
              <code>{data.data_dir}</code>
            </dd>
            <dt>{t("about.labels.reaperData")}</dt>
            <dd>{t("about.reaperDataDetail", { size: bytes(data.reaper_db_bytes) })}</dd>
            <dt>{t("about.labels.rebuildableCache")}</dt>
            <dd>{t("about.rebuildableCacheDetail", { size: bytes(data.cache_db_bytes) })}</dd>
          </dl>
        </div>
      )}
      {changesOpen && update.data && (
        <ChangesModal
          changes={update.data.changes}
          url={update.data.url}
          onClose={() => setChangesOpen(false)}
        />
      )}
    </div>
  );
}

/** The Update row's sentence, one branch per state the check can be in. Pending and a
 *  failed read each get their own branch. Both no-answer shapes, the HTTP call failing
 *  with nothing in hand and the server answering "unknown", read the same, since to the
 *  operator they are the same fact: no answer today, and nothing they must do.
 *
 *  A failed REFETCH is deliberately not a branch. React Query keeps the last good
 *  answer and sets `isError` beside it, and the pill, the chip light, and the dev banner
 *  all render that retained answer, so this row must too. Otherwise the pill could say
 *  "Update available" directly above a row claiming the check failed, the same stale-read
 *  split the `about` query above handles. */
function UpdateCell({
  status,
  onSeeChanges,
}: {
  status: ReturnType<typeof useUpdateStatus>;
  onSeeChanges: () => void;
}) {
  const { t } = useTranslation();
  const { data, isPending } = status;
  // The two no-answer shapes read the same (see above), so the sentence is written once
  // here instead of copied into each branch that needs it. "Later" refers to the
  // scheduled check (Settings, Jobs), which is what actually retries the read.
  const noAnswer = <span className="muted">{t("about.update.noAnswer")}</span>;
  if (isPending) return <span className="muted">{t("about.update.checking")}</span>;
  if (!data) return noAnswer;
  if (!data.enabled) return <span className="muted">{t("about.update.disabled")}</span>;
  if (data.update_available === null) return noAnswer;
  if (!data.update_available)
    return (
      <span>
        {data.channel === "dev"
          ? t("jobs.result.update_dev_current")
          : t("jobs.result.update_up_to_date")}
      </span>
    );
  if (data.channel === "dev")
    return (
      <>
        {t("jobs.result.update_dev_behind")}{" "}
        {data.url && (
          <a href={data.url} target="_blank" rel="noreferrer">
            {t("about.update.seeChanges")}
          </a>
        )}
        <br />
        <span className="muted">{t("about.update.devSteadier")}</span>
      </>
    );
  return (
    <>
      {t("about.update.newRelease", { version: data.latest })}{" "}
      <button type="button" className="link-btn" onClick={onSeeChanges}>
        {t("about.update.seeChanges")}
      </button>
      <br />
      {/* Points at the schedule rather than naming a cadence: the operator can change the
          cron or turn the job off, so a sentence saying "daily" would go wrong the moment
          they do. */}
      <span className="muted">{t("about.update.schedule")}</span>
    </>
  );
}

/** The GitHub changelog for every release the operator has not taken, newest first, in
 *  the one modal shell. The markdown renders sanitized: react-markdown emits no raw
 *  HTML, images are dropped so opening a note cannot load anything from another host,
 *  and headings are demoted below the dialog's own so the outline stays honest. Every
 *  link inside opens GitHub in a new tab. */
function ChangesModal({
  changes,
  url,
  onClose,
}: {
  changes: ReleaseChange[];
  url: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <ModalShell title={t("about.changesModal.title")} onClose={onClose} className="modal-changes">
      <div className="changes-body">
        {changes.length === 0 && (
          <p className="muted">
            {url ? (
              <Trans
                i18nKey="about.changesModal.noNotesWithLink"
                components={{ link: <a href={url} target="_blank" rel="noreferrer" /> }}
              />
            ) : (
              <Trans i18nKey="about.changesModal.noNotesNoLink" />
            )}
          </p>
        )}
        {changes.map((c) => (
          <section key={c.version} className="changes-release">
            <h3>{t("about.reaperVersion", { version: c.version })}</h3>
            {c.notes ? (
              <div className="changes-notes">
                <Markdown
                  // Drop <img> tags: a rendered <img> would fetch from whatever host the
                  // note names, just from being displayed.
                  disallowedElements={["img"]}
                  components={{
                    a: ({ node: _node, ...props }) => (
                      <a {...props} target="_blank" rel="noreferrer" />
                    ),
                    // GitHub's generated notes start at h2. Left alone, that would outrank
                    // the per-release h3 above it and match the dialog's own title.
                    h1: ({ node: _node, ...props }) => <h4 {...props} />,
                    h2: ({ node: _node, ...props }) => <h4 {...props} />,
                    h3: ({ node: _node, ...props }) => <h4 {...props} />,
                    h4: ({ node: _node, ...props }) => <h4 {...props} />,
                    h5: ({ node: _node, ...props }) => <h4 {...props} />,
                    h6: ({ node: _node, ...props }) => <h4 {...props} />,
                  }}
                >
                  {c.notes}
                </Markdown>
              </div>
            ) : (
              <p className="muted">{t("about.changesModal.noNotesForRelease")}</p>
            )}
            {c.url && (
              <p className="changes-link">
                <a href={c.url} target="_blank" rel="noreferrer">
                  {t("about.changesModal.viewOnGithub")}
                </a>
              </p>
            )}
          </section>
        ))}
      </div>
    </ModalShell>
  );
}
