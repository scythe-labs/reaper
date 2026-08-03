// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The first-run setup flow: four steps, one card at a time.
//
// It used to be one screen -- a checklist with the whole Settings services panel and the whole
// Plex panel inlined under it -- which is every decision a fresh install has to make, stacked
// in a single scroll. The steps exist to put one decision on screen at a time, in the order a
// new install actually needs them: claim the instance, point Reaper at Plex, point it at the
// services it reads from, then scan.
//
// **Where the operator resumes is derived from the server, never from this browser.** Each
// step declares what "already done" means for it (`IS_DONE` below) and the flow opens on the
// first one that is not; closing the tab mid-setup therefore comes back to the same place, and
// a step finished from another window is not offered again. Local state takes over once they
// are here, so Back and Skip move without re-asking the server what it thinks.
//
// The card wears `.modal`'s chrome on the page rather than being a real modal, and `ModalShell`
// is deliberately not reused: its contract is dismissal and returning focus to whatever opened
// it, and on the first screen of a fresh install there is no page behind it and no opener. The
// first step cannot be dismissed at all. Modals still appear *over* a step -- the service
// editor, Discord, the restore card -- and those are real `ModalShell` dialogs.

import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, type SetupStatus } from "../api";
import { BrandMark } from "../brand/BrandMark";
import { Notice } from "./Notice";
import { SetupConnectStep } from "./SetupConnectStep";
import { SetupPasswordStep } from "./SetupPasswordStep";
import { SetupPlexStep } from "./SetupPlexStep";
import { SetupScanStep } from "./SetupScanStep";
import { SETUP_STEPS, StepMovedProvider, type SetupStepKey } from "./SetupStepper";

/** What each step counts as finished, read off the server's setup status.
 *
 *  The whole resume rule, in one place, so the flow and the progress row cannot come to
 *  disagree about what "done" means. Plex counts as done only once it is linked: an operator
 *  who skipped it is offered it again next visit, which is deliberate, because without Plex
 *  `executor.execute` refuses every real run and the queue they are about to fill can never be
 *  acted on. One click skips it again.
 */
const IS_DONE: Record<SetupStepKey, (s: SetupStatus) => boolean> = {
  password: (s) => s.has_password,
  plex: (s) => s.plex_linked,
  connect: (s) => s.scan_ready,
  scan: (s) => s.has_scanned,
};

/** The first step that is not finished, or the last one when they all are. */
function firstOpenStep(status: SetupStatus): SetupStepKey {
  const open = SETUP_STEPS.find((s) => !IS_DONE[s.key](status));
  // `SETUP_STEPS` is a non-empty `as const`, but its index signature is not, so the fallback
  // names the last step rather than indexing for it.
  return open ? open.key : "scan";
}

export function SetupWizard({ onSkip }: { onSkip: () => void }) {
  const { data: setup, isError } = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });

  // Null until the first status lands, then seeded from it below and never re-derived: after
  // that this is the operator's own navigation, and re-deriving would drag them forward out of
  // a step they had gone Back to.
  const [step, setStep] = useState<SetupStepKey | null>(null);
  // The password typed on step one, so the restore door on a later step can confirm with it
  // rather than asking for it again. Never persisted; it dies with the flow.
  const [password, setPassword] = useState<string | null>(null);

  // Seeded from the server the first time a status lands, then never again. It used to be
  // derived inline on every render while `step` was null, which is the hazard the comment
  // above names: each step invalidates `["setup"]` on success, so linking Plex or saving the
  // last service moved the operator forward with no press -- off the Plex step before its
  // Libraries picker had ever rendered, and off Connect before they had seen the restore door.
  if (step === null && setup) setStep(firstOpenStep(setup));
  const at: SetupStepKey | null = step;

  // A ref, not state: it is read during the render that mounts the next card, and it must not
  // itself cause one. Set here rather than beside `setStep` above, so the seeded first step
  // counts as arriving rather than as moving.
  const moved = useRef(false);
  const go = (next: SetupStepKey) => {
    moved.current = true;
    setStep(next);
    window.scrollTo({ top: 0 });
  };
  const goNext = (from: SetupStepKey) => {
    const i = SETUP_STEPS.findIndex((s) => s.key === from);
    const next = SETUP_STEPS[i + 1];
    if (next) go(next.key);
  };

  // Still waiting and couldn't-read are different states, and neither may trap the owner:
  // App.tsx routes an unreadable setup status here on the promise that leaving still works, so
  // the failure branch carries a way out (rule 17/36). `main`, not a div, on every branch --
  // this screen replaces the whole app shell, so its landmarks are the only ones on the page.
  if (!setup || at === null) {
    return (
      <main className="setup">
        <Brand />
        <section className="step-card">
          <div className="step-head">
            <h2>{isError ? "Welcome to Reaper" : "Setting things up…"}</h2>
          </div>
          {isError && (
            <>
              <Notice tone="error">
                Couldn't check what's set up yet. You can go to the app and finish from Settings.
              </Notice>
              <div className="step-actions">
                <span className="spacer" />
                <button className="primary" onClick={onSkip}>
                  Go to the app
                </button>
              </div>
            </>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="setup">
      <Brand />
      <StepMovedProvider moved={moved.current}>
        {at === "password" && (
          <SetupPasswordStep
            onDone={(pw) => {
              setPassword(pw);
              goNext("password");
            }}
          />
        )}
        {at === "plex" && <SetupPlexStep setup={setup} onNext={() => goNext("plex")} />}
        {at === "connect" && (
          <SetupConnectStep
            setup={setup}
            password={password}
            onBack={() => go("plex")}
            onNext={() => goNext("connect")}
          />
        )}
        {at === "scan" && (
          <SetupScanStep
            setup={setup}
            onBack={() => go("connect")}
            onGoToPlex={() => go("plex")}
            onDone={onSkip}
          />
        )}
      </StepMovedProvider>
    </main>
  );
}

function Brand() {
  return (
    <div className="setup-brand">
      <BrandMark className="brand-mark" />
      <span className="brand-word">Reaper</span>
    </div>
  );
}
