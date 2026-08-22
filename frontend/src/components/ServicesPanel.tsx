// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Services: the Radarr, Sonarr, Tautulli and Seerr connections Reaper reads from, and
// the two of them it deletes through, one card per configured instance.
//
// Nothing is edited in place. A card opens `ServiceModal`, whose scrim covers the section rail,
// so no draft here can be lost to a section switch and this panel reports none upward
// (`dirtyPanels` in Settings.tsx says so, and a draft added to the panel BEHIND the modal
// would have to start reporting).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type RefObject, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { announce } from "../announce";
import { useSuccessorFocus } from "../focus";
import { api, type InstanceKind, type Instance, type InstanceTest } from "../api";
import { useBackGuard } from "../backnav";
import { describeError } from "../errors";
import { kinds, kindLabel, ServiceModal, TestBadge, testSentence } from "./ServiceModal";
import { StaleReadNotice } from "./StaleReadNotice";
import { Notice } from "./Notice";

function ServiceCard({
  instance,
  onEdit,
  onRemoving,
}: {
  instance: Instance;
  onEdit: () => void;
  /** Called as the remove is sent, so the section above can catch focus when this card goes.
   *  Lives up there rather than here because the successor is not inside this card -- the card
   *  IS what unmounts (#173). */
  onRemoving?: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // The result and the address it was computed for, the third of this badge's siblings to keep the
  // pairing (rule 72; `ServiceModal` and the Discord row are the others). Nothing cleared this, and
  // editing a service through the modal invalidates `instances`, so the card re-rendered with a new
  // address while the local result went on vouching for the old one (rule 85, #178).
  //
  // The address and the certificate setting are what this card can see change. A key ROTATED at the
  // same address is not visible here -- `has_key` stays true -- so it is included for the false-to-
  // true case only. The rotation is answered on the server instead: `update_instance` clears
  // `last_ok_at` / `last_error` / `detected_version` whenever the address, the key or the
  // certificate setting changes, so the fallbacks below cannot outlive what they were computed
  // against either, and this card drops to "Not tested yet" (#264, rule 85).
  const [test, setTest] = useState<{ result: InstanceTest; of: string } | null>(null);
  const testedWith = () => `${instance.base_url} ${instance.verify_tls} ${instance.has_key}`;
  // A two-step "Remove" -> "Confirm remove" toggle, mirroring the arm confirm in
  // `DeletionToggle.tsx`, rather than a native confirm() dialog -- the OS alert box ignores the
  // app's theme and typography and is the only confirmation in the product that does.
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["instances"] });
    void queryClient.invalidateQueries({ queryKey: ["setup"] });
  };

  const testSaved = useMutation({
    mutationFn: () => api.testSavedInstance(instance.id),
    // Announced, like `ServiceModal`'s own test and the webhook's in `NotificationsPanel` --
    // three siblings of one
    // result that reached nobody by ear (#192, rule 72). `testSentence` is the one wording.
    //
    // What this request is ABOUT, captured when it is issued rather than computed at success
    // time, the same as those siblings. Here the fingerprint moves under a REFETCH, not under
    // typing. The Edit modal opens over this card, and saving a new address there invalidates
    // `instances`. A test still in flight for the old address would settle stamped with the new
    // one, and the card would read "Reached" for a host nobody tried.
    onMutate: () => ({ of: testedWith() }),
    onSuccess: (r, _v, issued) => {
      setTest({ result: r, of: issued.of });
      announce(testSentence(r));
      invalidate();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteInstance(instance.id),
    // Adding a service speaks and removing one did not, which is the asymmetry #192 was about:
    // the whole card vanishes, and an absence cannot be perceived by ear.
    onSuccess: () => {
      announce(t("services.panel.card.removedAnnouncement", { name: instance.name }));
      invalidate();
    },
  });

  const certCheckOff = !instance.verify_tls && instance.base_url.startsWith("https://");

  return (
    <article className={`service-card ${instance.enabled ? "" : "disabled"}`}>
      <div className="service-card-body">
        <div className="instance-id">
          <span className={`kind-badge kind-${instance.kind}`}>{kindLabel(instance.kind)}</span>
          <strong>{instance.name}</strong>
          {!instance.enabled && (
            <span className="chip">{t("services.panel.card.disabledChip")}</span>
          )}
          {certCheckOff && (
            <span className="chip chip-warn">{t("services.panel.card.certCheckOffChip")}</span>
          )}
        </div>
        <div className="instance-url muted">{instance.base_url}</div>
        <div className="instance-status">
          {/* All three states render through the one badge. What the card remembers from
              the last test is the same shape a fresh test returns, so it is handed over as
              one rather than rebuilt with a second set of markup that can drift. */}
          {test && test.of === testedWith() ? (
            <TestBadge result={test.result} />
          ) : instance.last_error ? (
            <TestBadge result={{ ok: false, detail: instance.last_error, version: null }} />
          ) : instance.last_ok_at ? (
            <TestBadge
              result={{
                ok: true,
                detail: t("services.panel.card.reachedDetail"),
                version: instance.detected_version,
              }}
            />
          ) : (
            <span className="muted">{t("services.panel.card.notTestedYet")}</span>
          )}
        </div>
        {(remove.error ?? testSaved.error) && (
          <Notice tone="error" inline>
            {remove.error
              ? t("services.panel.card.removeFailed", { message: describeError(remove.error) })
              : t("services.panel.card.testFailed", {
                  message: testSaved.error ? describeError(testSaved.error) : "",
                })}
          </Notice>
        )}
      </div>
      {/* Each button carries the instance it acts on. A tester with two Sonarr, two Radarr and a
          Tautulli renders this card five times, and by their visible text alone the buttons are
          five identical "Test"/"Edit"/"Remove" triplets with nothing saying which service each
          one would remove. The name is on screen at `.instance-id` above, but nothing bound it
          to the controls. */}
      <div className="service-card-foot">
        {confirmingRemove ? (
          <>
            <button
              type="button"
              className="danger"
              title={t("services.panel.card.confirmRemoveTitle")}
              aria-label={t("services.panel.card.confirmRemoveAria", { name: instance.name })}
              onClick={() => {
                setConfirmingRemove(false);
                onRemoving?.();
                remove.mutate();
              }}
            >
              {t("common.confirmRemove")}
            </button>
            <button
              type="button"
              // The visible word first. A name that drops it entirely leaves a voice-control
              // operator saying "click Cancel" at a button that answers to "Keep", with the
              // red Confirm remove as the only other control in reach.
              aria-label={t("services.panel.card.cancelRemoveAria", { name: instance.name })}
              onClick={() => setConfirmingRemove(false)}
            >
              {t("common.cancel")}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={testSaved.isPending}
              // Carries the state, because the name REPLACES the visible text rather than
              // extending it: a fixed name freezes the button at "Test" while its label flips
              // to "Testing…", and nothing else here announces that the press did anything.
              // Visible word first, so "click Test" still reaches it by voice.
              aria-label={
                testSaved.isPending
                  ? t("services.panel.card.testAriaPending", { name: instance.name })
                  : t("services.panel.card.testAriaIdle", { name: instance.name })
              }
              onClick={() => testSaved.mutate()}
            >
              {testSaved.isPending
                ? t("services.common.testing")
                : t("services.panel.card.testButton")}
            </button>
            <button
              type="button"
              aria-label={t("common.editNamed", { name: instance.name })}
              onClick={onEdit}
            >
              {t("common.edit")}
            </button>
            <button
              type="button"
              className="danger"
              aria-label={t("services.panel.card.removeAria", { name: instance.name })}
              onClick={() => setConfirmingRemove(true)}
            >
              {t("common.remove")}
            </button>
          </>
        )}
      </div>
    </article>
  );
}

/** One kind's cards and its Add button.
 *
 *  Its own component only so it can hold a hook per kind: a `useSuccessorFocus()` inside the
 *  `kinds().map` below would be a hook in a loop. What it buys is where focus goes when a card
 *  removes itself -- the card IS the thing that unmounts, so the successor cannot live inside it
 *  (#173). The Add button is the target rather than a neighbouring card: the cards' own focusable
 *  content is a Test/Edit/Remove triplet, and landing on another service's Test button reads as
 *  the wrong service being acted on, where Add is the one thing left to do in this section. It is
 *  also the only candidate that is always there -- removing a singleton kind's one card empties
 *  the grid, and `canAdd` flips the Add button ON in the same refetch, which the hook waits for. */
function ServiceSection({
  kind,
  rows,
  onOpen,
}: {
  kind: ReturnType<typeof kinds>[number];
  rows: Instance[];
  onOpen: (instance: Instance | null) => void;
}) {
  const { t } = useTranslation();
  const afterRemove = useSuccessorFocus();
  // A singleton kind (Tautulli) shows no "Add" once one exists: it mirrors one Plex, and
  // Reaper connects to one Plex, so a second has no working setup.
  const canAdd = !kind.singleton || rows.length === 0;
  return (
    <section className="service-section">
      <h3>{kind.label}</h3>
      <p className="service-hint">{kind.hint}</p>
      <div className="service-grid">
        {rows.map((i) => (
          <ServiceCard
            key={i.id}
            instance={i}
            onEdit={() => onOpen(i)}
            onRemoving={afterRemove.arriving}
          />
        ))}
        {canAdd && (
          <button
            type="button"
            className="service-add"
            ref={afterRemove.ref as RefObject<HTMLButtonElement>}
            onClick={() => onOpen(null)}
          >
            <span aria-hidden="true">+</span>{" "}
            {t("services.panel.addKindButton", { label: kind.label })}
          </button>
        )}
      </div>
    </section>
  );
}

export function ServicesPanel() {
  const { t } = useTranslation();
  const { data, isPending, error } = useQuery({ queryKey: ["instances"], queryFn: api.instances });
  const [modal, setModal] = useState<{ kind: InstanceKind; instance: Instance | null } | null>(
    null,
  );
  // The modal decides when it may be dismissed; it mirrors that whole answer here so Back
  // refuses exactly what the scrim, Escape and the ✕ refuse, the same arrangement the schedule
  // editor uses (B-19). It carried only the SAVE half once, and the moment the modal grew a
  // second reason to stay open -- a folder map read but never made -- Back walked through the
  // new guard while every other dismissal honored it (rule 80).
  const blockCloseRef = useRef(false);
  // Back closes the service editor instead of leaving Reaper -- unless the modal says otherwise.
  useBackGuard(
    modal !== null,
    () => setModal(null),
    () => !blockCloseRef.current,
  );

  return (
    <div className="panel panel-wide">
      <h2>{t("services.panel.heading")}</h2>
      {/* "It only ever reads" was false about Radarr and Sonarr, which are how a reap removes
          anything: the executor unmonitors, deletes files and adds exclusions through them.
          Bounded to the scan, which is what the claim was reaching for. Its twin is the
          wizard's Connect step (rule 72). */}
      <p className="blurb">{t("services.panel.blurb")}</p>
      {/* The one Settings panel #140 did not reach. A raw exception string over the full service
          list broke rule 21 on its own, and said the read had failed above the connections it
          had read (#190): saving or removing an instance invalidates ["instances"], so an
          ordinary edit reaches it. */}
      {error && !data && <Notice tone="error">{t("services.panel.loadError")}</Notice>}
      {error && data && <StaleReadNotice what={t("services.panel.staleWhat")} />}
      {isPending && <p className="muted">{t("common.loading")}</p>}
      {data &&
        kinds().map((k) => (
          <ServiceSection
            key={k.value}
            kind={k}
            rows={data.filter((i) => i.kind === k.value)}
            onOpen={(instance) => setModal({ kind: instance?.kind ?? k.value, instance })}
          />
        ))}
      {modal && (
        <ServiceModal
          key={modal.instance ? modal.instance.id : `add-${modal.kind}`}
          kind={modal.kind}
          instance={modal.instance}
          onClose={() => setModal(null)}
          blockCloseRef={blockCloseRef}
        />
      )}
    </div>
  );
}
