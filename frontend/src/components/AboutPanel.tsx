// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> About: which version is running, what changed in the ones after it, and how much
// room the database is taking.
//
// Read-only, so it holds no draft and `dirtyPanels` in Settings.tsx classifies it `false`. The
// update check is the shared `useUpdateStatus` the masthead chip's light reads, so that light and
// the row here answer from one read rather than two that can disagree. The *pill* is local to this
// file, which is why the note further down names all three surfaces apart.
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
      {/* standing: which channel this build is on is a fact about the install, true on
          first paint and unchanged for the process's whole life -- page furniture, not a
          reaction to anything pressed. */}
      {update.data?.channel === "dev" && (
        <Notice tone="warn" standing>
          <Trans i18nKey="about.devBuildWarning" components={{ code: <code /> }} />
        </Notice>
      )}
      {isPending && <p className="muted">{t("common.loading")}</p>}
      {/* Two cases, not one. React Query keeps the last good row through a failed refetch and
          raises isError beside it, so an undivided `isError` printed "couldn't load this page"
          directly above the fully drawn page (rule 17/36). The trigger is a remount past
          `staleTime` -- leaving About and coming back 30 seconds later while the server is
          unreachable. Not window focus, which `main.tsx` turns off app-wide and only `useSafety`
          asks back, and not an invalidation: nothing in the app invalidates `["about"]`. */}
      {isError && !data && <Notice tone="error">{t("common.pageLoadError")}</Notice>}
      {isError && data && <StaleReadNotice what={t("about.staleReadWhat")} />}
      {data && (
        <div className="set-rows">
          <dl className="about-kv">
            <dt>{t("about.labels.version")}</dt>
            <dd>
              {t("about.reaperVersion", { version: data.version })}
              {update.data?.update_available && (
                <span className="update-pill">
                  {update.data.channel === "dev"
                    ? t("about.updatePill.newerDevBuild")
                    : t("about.updatePill.updateAvailable")}
                </span>
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
 *  failed read are spelled out (rule 17/36), and both no-answer shapes -- the HTTP
 *  call failing with nothing in hand, and the server answering "unknown" -- read the
 *  same, because to the operator they are the same fact: no answer today, and nothing
 *  they must do.
 *
 *  A failed REFETCH is deliberately not a branch: React Query keeps the last good
 *  answer and raises `isError` beside it, and the pill, the chip light, and the dev
 *  banner all render that retained answer -- so this row must too, or the pill says
 *  "Update available" directly above a row claiming the check failed (the exact
 *  stale-read split the `about` query above documents). */
function UpdateCell({
  status,
  onSeeChanges,
}: {
  status: ReturnType<typeof useUpdateStatus>;
  onSeeChanges: () => void;
}) {
  const { t } = useTranslation();
  const { data, isPending } = status;
  // The two no-answer shapes read the same (see above), so the sentence is written once:
  // one operator claim in two places is two chances to drift (rule 144). "Later" is the
  // scheduled check (Settings, Jobs), which is what makes the promise real -- before it
  // existed nothing retried on a server nobody opened (#464).
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
      {/* Points at the schedule rather than naming one: the operator can change the cron
          or turn the job off, so a sentence saying "daily" is wrong the moment they do
          (rule 86). This used to say "Reaper checks a few times a day" while nothing
          checked on its own at all (#464, rule 25). */}
      <span className="muted">{t("about.update.schedule")}</span>
    </>
  );
}

/** The GitHub changelog for every release the operator has not taken, newest first, in
 *  the one modal shell. The markdown is rendered sanitized -- react-markdown emits no
 *  raw HTML, images are dropped so a note cannot phone home just for being read, and
 *  headings are demoted under the dialog's own so the outline stays honest. Every
 *  link inside leaves for GitHub in a new tab. */
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
                  // Images out: a rendered <img> fetches from wherever the note says,
                  // beside copy promising nothing leaves the box just for reading.
                  disallowedElements={["img"]}
                  components={{
                    a: ({ node: _node, ...props }) => (
                      <a {...props} target="_blank" rel="noreferrer" />
                    ),
                    // GitHub's generated notes open at h2; undemoted that outranks the
                    // per-release h3 and sits level with the dialog's own name.
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
