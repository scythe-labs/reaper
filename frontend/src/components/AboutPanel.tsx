// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> About: which version is running, what changed in the ones after it, and how much
// room the database is taking.
//
// Read-only, so it holds no draft and `dirtyPanels` in Settings.tsx classifies it `false`. The
// update check is the shared `useUpdateStatus` the masthead chip's light reads, so that light and
// the row here answer from one read rather than two that can disagree. The *pill* is local to this
// file, which is why the note further down names all three surfaces apart.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type ReleaseChange } from "../api";
import Markdown from "react-markdown";
import { useUpdateStatus } from "../updateStatus";
import { bytes } from "../format";
import { ModalShell } from "./ModalShell";
import { StaleReadNotice } from "./StaleReadNotice";
import { Notice } from "./Notice";

export function AboutPanel() {
  const { data, isPending, isError } = useQuery({ queryKey: ["about"], queryFn: api.about });
  const update = useUpdateStatus();
  const [changesOpen, setChangesOpen] = useState(false);

  return (
    <div className="panel">
      <h2>About</h2>
      <p className="blurb">What's running, and where its data lives.</p>
      {/* standing: which channel this build is on is a fact about the install, true on
          first paint and unchanged for the process's whole life -- page furniture, not a
          reaction to anything pressed. */}
      {update.data?.channel === "dev" && (
        <Notice tone="warn" standing>
          You are running a <code>dev</code> build of Reaper. It changes daily and can break; use a
          release unless you are helping test.
        </Notice>
      )}
      {isPending && <p className="muted">Loading…</p>}
      {/* Two cases, not one. React Query keeps the last good row through a failed refetch and
          raises isError beside it, so an undivided `isError` printed "couldn't load this page"
          directly above the fully drawn page (rule 17/36). The trigger is a remount past
          `staleTime` -- leaving About and coming back 30 seconds later while the server is
          unreachable. Not window focus, which `main.tsx` turns off app-wide and only `useSafety`
          asks back, and not an invalidation: nothing in the app invalidates `["about"]`. */}
      {isError && !data && (
        <Notice tone="error">Couldn't load this page. Reload to try again.</Notice>
      )}
      {isError && data && <StaleReadNotice what="these details" />}
      {data && (
        <div className="set-rows">
          <dl className="about-kv">
            <dt>Version</dt>
            <dd>
              Reaper {data.version}
              {update.data?.update_available && (
                <span className="update-pill">
                  {update.data.channel === "dev" ? "Newer dev build" : "Update available"}
                </span>
              )}
            </dd>
            <dt>Update</dt>
            <dd>
              <UpdateCell status={update} onSeeChanges={() => setChangesOpen(true)} />
            </dd>
            <dt>License</dt>
            <dd>{data.license}</dd>
            <dt>Data folder</dt>
            <dd>
              <code>{data.data_dir}</code>
            </dd>
            <dt>Reaper's own data</dt>
            <dd>{bytes(data.reaper_db_bytes)}, decisions, audit trail, credentials</dd>
            <dt>Rebuildable cache</dt>
            <dd>{bytes(data.cache_db_bytes)}, watch history, ratings, lists</dd>
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
  const { data, isPending } = status;
  // The two no-answer shapes read the same (see above), so the sentence is written once:
  // one operator claim in two places is two chances to drift (rule 144). "Later" is the
  // scheduled check (Settings, Jobs), which is what makes the promise real -- before it
  // existed nothing retried on a server nobody opened (#464).
  const noAnswer = (
    <span className="muted">Couldn't check for updates. Reaper will try again later.</span>
  );
  if (isPending) return <span className="muted">Checking for updates…</span>;
  if (!data) return noAnswer;
  if (!data.enabled)
    return (
      <span className="muted">
        Update checks are off, so Reaper never asks GitHub for versions. Remove REAPER_UPDATE_CHECK
        from launcher.conf in Reaper's data folder, or from your environment, to turn them back on.
      </span>
    );
  if (data.update_available === null) return noAnswer;
  if (!data.update_available)
    return (
      <span>
        {data.channel === "dev"
          ? "This build matches the dev branch."
          : "You are on the newest release."}
      </span>
    );
  if (data.channel === "dev")
    return (
      <>
        The dev branch has moved since this build.{" "}
        {data.url && (
          <a href={data.url} target="_blank" rel="noreferrer">
            See what changed
          </a>
        )}
        <br />
        <span className="muted">Dev builds change often. Releases are the steadier channel.</span>
      </>
    );
  return (
    <>
      Reaper {data.latest} is out.{" "}
      <button type="button" className="link-btn" onClick={onSeeChanges}>
        See what changed
      </button>
      <br />
      {/* Points at the schedule rather than naming one: the operator can change the cron
          or turn the job off, so a sentence saying "daily" is wrong the moment they do
          (rule 86). This used to say "Reaper checks a few times a day" while nothing
          checked on its own at all (#464, rule 25). */}
      <span className="muted">
        Reaper checks on a schedule you can change in Jobs, and never sends anything about your
        library.
      </span>
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
  return (
    <ModalShell title="What changed" onClose={onClose} className="modal-changes">
      <div className="changes-body">
        {changes.length === 0 && (
          <p className="muted">
            No release notes to show.{" "}
            {url ? (
              <a href={url} target="_blank" rel="noreferrer">
                The release page on GitHub
              </a>
            ) : (
              "The releases page on GitHub"
            )}{" "}
            has the full story.
          </p>
        )}
        {changes.map((c) => (
          <section key={c.version} className="changes-release">
            <h3>Reaper {c.version}</h3>
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
              <p className="muted">This release shipped without notes.</p>
            )}
            {c.url && (
              <p className="changes-link">
                <a href={c.url} target="_blank" rel="noreferrer">
                  View on GitHub
                </a>
              </p>
            )}
          </section>
        ))}
      </div>
    </ModalShell>
  );
}
