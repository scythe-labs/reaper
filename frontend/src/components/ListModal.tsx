// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Add or edit one protection list. Settings defines what a list is and where it comes from.
// Policy defines what it does, through a keep rule naming the list.
//
// Adding walks two steps, the way the *arrs add an import list: a type picker (Plex
// collection, Plex watchlist, Sonarr and Radarr tags, IMDb), then the one form that type
// needs. The source is chosen once and is fixed afterwards. The stored membership is keyed
// on a slug that carries the source, so re-pointing a Plex collection at an *arr tag would
// leave the old membership enabled under the old slug, still protecting from a definition
// the operator has already replaced. That is the failure `retire_absent` exists to prevent.
//
// Removing lives inside Edit, as the third view of this one modal, so a row's actions stay
// two buttons, and the destructive one sits behind the form that names what it destroys.
//
// The `blocked` sentences below are a second copy of refusals `services.list_config` also
// writes, said here so the operator reads them while looking at the empty box rather than
// after a round trip. Keeping two copies of one requirement in sync is a real risk: each side
// can be pinned by its own test with nothing binding the pair, so a one-sided edit can leave
// both suites green. `tests/test_list_config.py` names this file in the failure message that
// fires when the server's wording moves, so the copies cannot drift silently.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import { api, type ListConfig, type ListConfigBody } from "../api";
import { useBackCloseMirror, useBackGuard } from "../backnav";
import { describeError } from "../errors";
import i18next from "../i18n";
import { usePlexLibraries } from "../usePlexLibraries";
import { FilterMenu } from "./FilterMenu";
import { ModalShell } from "./ModalShell";
import { Notice } from "./Notice";
import { Segmented } from "./Segmented";
import { TagsEditor } from "./TagsEditor";

type Source = ListConfig["source"];

/** Which control the "why Add is off" sentence is about, so it can be bound to that control. */
type BlockedField = "name" | "library" | "collection" | "tags" | "imdb";

const BLOCKED_ID = "list-modal-blocked";

/** How long a list name may be, the number `ListConfigIn.name` and `list_config._clean_name`
 *  both hold. Exported so the pairing test can read it rather than transcribe it. The
 *  schema's bound is checked first, so `_clean_name`'s "That name is too long" only ever
 *  reaches an operator who got past this box. */
export const LIST_NAME_MAX = 100;

/** What the operator is told each source is, in their words. */
function sourceName(source: Source): string {
  return i18next.t("lists.sourceName", { source });
}

/** The shipped IMDb charts' keys, the server's own spelling (`services.lists.IMDB_PRESETS`).
 *  No route serves them, so this is the one browser copy and it is checked by the tests that
 *  post each key. A stored preset this table does not know still renders, by its raw key
 *  (`presetLabel`'s fallback). */
const IMDB_PRESETS: readonly string[] = ["top250", "popular"];

function presetLabel(key: string): string {
  return i18next.t("lists.presetLabel", { key });
}

/** One card in the type picker. */
function PickCard({
  name,
  blurb,
  cardRef,
  children,
}: {
  name: string;
  blurb: string;
  /** For a card holding a popover: the outside-click close checks containment on this. */
  cardRef?: React.Ref<HTMLDivElement>;
  children: React.ReactNode;
}) {
  return (
    <div className="pick-card" ref={cardRef}>
      <span className="nm">{name}</span>
      <p>{blurb}</p>
      {children}
    </div>
  );
}

export function ListModal({
  editing,
  onClose,
  onSaved,
  onChanged,
  blockCloseRef,
}: {
  editing: ListConfig | null;
  onClose: () => void;
  /** The stored row, handed back so the panel can check it straight away. A list nobody has
   *  read yet protects nothing, so leaving the first check to the operator makes "save" and
   *  "protected" two different moments with a button in between. The check runs there rather
   *  than here: this modal is unmounting, and the row already owns a check's whole reporting
   *  surface, its busy button and its own error line. */
  onSaved?: ((list: ListConfig) => void) | undefined;
  /** Called once the registry has actually changed, by every path that changes it: add,
   *  edit and remove alike. Separate from `onSaved` because removing changes what protects
   *  the library just as much as saving does and hands back no row to check.
   *
   *  `rescore` says whether the change moved what a keep rule protects, which is the only kind
   *  of change the queue's stored fates were scored under. It is true when a rule names the
   *  list (an edit or a remove of a used list), and false for a list nothing uses, every add
   *  included, since an added list carries no rule. The panel starts a scan only when it is
   *  true, so adding a list the operator has not wired to Policy does not kick off a full
   *  library scan for nothing.
   *
   *  The panel does the acting, for the same reason the check runs there: this modal is
   *  unmounting, and a mutation started on the way out loses the surface that would report
   *  it. */
  onChanged?: ((rescore: boolean) => void) | undefined;
  /** The panel's mirror of `canClose`, so its Back guard refuses exactly what the scrim,
   *  Escape and the close button refuse, rather than a stale copy of one of the reasons.
   *  Written by `useBackCloseMirror`, which is the only writer of any of these. */
  blockCloseRef?: RefObject<boolean> | undefined;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // The any/all pair, there from the start so the form never has a blank where a control
  // belongs. This uses the shared `Segmented` in its flat variant, the mockup's chrome, one
  // either-or control.
  const MATCH_OPTIONS = [
    ["any", t("lists.matchAny")],
    ["all", t("lists.matchAll")],
  ] as const satisfies readonly (readonly ["any" | "all", string])[];

  // Adding opens on the type picker; editing goes straight to the form, and the remove
  // confirmation is the form's own third view rather than a second modal.
  const [view, setView] = useState<"picker" | "form" | "confirm">(editing ? "form" : "picker");
  const [source, setSource] = useState<Source>(editing?.source ?? "plex_collection");
  const [preset, setPreset] = useState<string | null>(editing?.config.preset ?? null);
  const [presetsOpen, setPresetsOpen] = useState(false);
  /** The IMDb card, which anchors the presets menu; a press outside it closes the menu. */
  const presetsRef = useRef<HTMLDivElement>(null);

  // Back closes the open presets menu rather than the layer beneath it, the same contract the
  // queue's filter popover and the spare menu keep (`ReviewQueue`, `OverrideControls`). Without
  // this, the press would skip the menu, land on the Settings section frame instead, and
  // navigate the panel behind the modal while the menu was still drawn.
  useBackGuard(presetsOpen, () => setPresetsOpen(false));

  // Close the open presets menu on an outside click or Escape, the queue's menu contract.
  useEffect(() => {
    if (!presetsOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!presetsRef.current?.contains(e.target as Node)) setPresetsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // The menu is the newest layer, so it consumes the press: ModalShell's Escape sits on
      // `window`, and without this stop the same press would tear down the whole modal.
      e.stopPropagation();
      setPresetsOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [presetsOpen]);

  const [name, setName] = useState(editing?.name ?? "");
  const [library, setLibrary] = useState(editing?.config.library ?? "");
  const [collection, setCollection] = useState(editing?.config.collection ?? "");
  const [tags, setTags] = useState<string[]>(editing?.config.tags ?? []);
  const [match, setMatch] = useState<"any" | "all">(editing?.config.match ?? "any");
  const [imdbId, setImdbId] = useState(editing?.config.list_id ?? "");

  const openForm = (next: Source) => {
    setSource(next);
    setPreset(null);
    // The watchlist form has nothing to set up, so the name is the one box. Give it a
    // starting value the operator can keep.
    setName(next === "plex_watchlist" ? t("lists.watchlistDefaultName") : "");
    setView("form");
  };
  const openPreset = (key: string) => {
    setSource("imdb");
    setPreset(key);
    setName(presetLabel(key));
    setView("form");
  };

  // The operator's real Plex libraries, so the one field that could make an install unable to
  // reap at all is picked rather than typed. A hardcoded library name would go unread the
  // moment a library is named anything else. This is optional and soft: see `libraryOptions`
  // for what happens when Plex cannot be asked.
  const { libraries } = usePlexLibraries({
    enabled: view === "form" && source === "plex_collection",
  });

  // The stored library is always among the choices, even when Plex no longer reports one by
  // that name. A select whose value is absent from its options renders as the FIRST option,
  // so a renamed or unreachable library would quietly re-point the operator's keep collection
  // at whichever library happens to sort first, and the save would write that. Keeping the
  // stored spelling makes the mismatch visible instead.
  const libraryOptions = (() => {
    const found = (libraries.data ?? []).map((l) => l.title);
    return library && !found.includes(library) ? [library, ...found] : found;
  })();
  // Plex answered and named at least one library. Until it does, the field is a text box:
  // an empty select is a form that cannot be filled in, and being unable to reach Plex must
  // not be the reason an operator cannot write down which collection protects their files.
  const canPickLibrary = libraries.isSuccess && libraryOptions.length > 0;

  const body = (): ListConfigBody => {
    if (source === "plex_collection") return { library, collection };
    if (source === "arr_tag") return { tags, match };
    if (source === "imdb") return preset ? { preset } : { list_id: imdbId };
    return {};
  };

  /** Whether any configuration field has moved since the form was seeded.
   *
   *  `ListConfigPatch` is omitted-means-keep, and `list_config.update` replaces only the
   *  fields it is given, so a save that sends `config` at all writes the whole thing back.
   *  This form seeds once, from a `lists-configured` row the cache may have held for a
   *  while: `main.tsx` sets `refetchOnWindowFocus: false` with a 30 second `staleTime`, and
   *  nothing refetches between the panel rendering and this modal opening. Saving
   *  unconditionally against that stale row would silently revert a collection someone else
   *  had repointed, or tags they had changed, in another tab.
   *
   *  This compares field by field against the same expressions that seeded each piece of
   *  state, which is what makes these the canonical forms a dirty check needs. Comparing the
   *  whole stored `config` instead would read a defaulted `match` as an edit on every save. */
  const configDirty =
    source !== (editing?.source ?? source) ||
    preset !== (editing?.config.preset ?? null) ||
    library !== (editing?.config.library ?? "") ||
    collection !== (editing?.config.collection ?? "") ||
    imdbId !== (editing?.config.list_id ?? "") ||
    match !== (editing?.config.match ?? "any") ||
    JSON.stringify(tags) !== JSON.stringify(editing?.config.tags ?? []);

  const save = useMutation({
    mutationFn: async () => {
      // Omitted, not sent unchanged: sending it is what makes this a write, and a write of
      // a value read minutes ago is the one that loses somebody else's edit.
      if (editing) {
        return api.editList(editing.id, configDirty ? { name, config: body() } : { name });
      }
      return api.addList(name, source, body());
    },
    onSuccess: async (saved) => {
      // Three halves: the definitions this modal wrote, the health rows keyed on them, and
      // the policies. Adding a list writes no rule, but a rename re-spells every rule naming
      // it, so a stale policy cache would render rules about a list name that no longer
      // exists.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["lists-configured"] }),
        queryClient.invalidateQueries({ queryKey: ["lists"] }),
        queryClient.invalidateQueries({ queryKey: ["policy"] }),
      ]);
      // After the refetch, so the row this names is already on screen to report the check.
      // This uses the server's row, not the form's fields, since it carries the id and is the
      // cleaned copy the save actually stored.
      onSaved?.(saved);
      // A rescan is warranted only when a keep rule names the list: an add writes none, so
      // `policy_use` is empty and no fate moved; an edit of a used list can move membership or
      // re-spell the rule, so it did.
      onChanged?.(saved.policy_use.length > 0);
      onClose();
    },
  });

  const remove = useMutation({
    mutationFn: () => api.removeList(editing!.id),
    onSuccess: async () => {
      // Same three: deleting a list deletes the keep rules naming it, in the same request.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["lists-configured"] }),
        queryClient.invalidateQueries({ queryKey: ["lists"] }),
        queryClient.invalidateQueries({ queryKey: ["policy"] }),
      ]);
      // Removing a list that a rule named drops that protection, so the queue must re-score.
      // Removing one nothing used changes no fate.
      onChanged?.((editing?.policy_use.length ?? 0) > 0);
      onClose();
    },
  });

  // What the submit button is waiting on. The server refuses each of these too, in the same
  // words; saying it here means the operator reads it while looking at the empty box rather
  // than after a round trip (the shape `_clean_config`'s docstring describes).
  // Which control the sentence is about, so it can be bound to that control rather than only
  // rendered beside a disabled button. `PolicyRuleEditors` does the same and says why: a
  // disabled submit is out of the Tab order, so the reason has to live on the box the operator
  // is standing in or it is announced nowhere at all.
  const blocked = ((): { on: BlockedField; why: string } | null => {
    if (!name.trim()) {
      return { on: "name", why: t("lists.blocked.name") };
    }
    if (source === "plex_collection") {
      if (!library.trim()) return { on: "library", why: t("lists.blocked.library") };
      if (!collection.trim()) {
        return { on: "collection", why: t("lists.blocked.collection") };
      }
      return null;
    }
    if (source === "arr_tag") {
      return tags.length === 0 ? { on: "tags", why: t("lists.blocked.tags") } : null;
    }
    if (source === "imdb" && !preset && !imdbId.trim()) {
      return { on: "imdb", why: t("lists.blocked.imdb") };
    }
    return null;
  })();
  const missing = blocked?.why ?? null;
  /** `aria-describedby` for the one control the blocking sentence is about. */
  const describedBy = (field: BlockedField) => (blocked?.on === field ? BLOCKED_ID : undefined);

  /** Whether a dismissal is allowed, computed once and handed to every path that can dismiss.
   *
   *  A close mid-save would unmount the only place the refusal is ever shown: the scrim would
   *  swallow the server's sentence, the invalidations would never run, and the operator would
   *  walk away believing the list saved.
   *
   *  Cancel is deliberately not gated on this, unlike `ServiceModal`, where both Cancels are
   *  disabled while their mutation is in flight. Disabling Cancel here too would mean that in
   *  the one state this guard refuses a close, the scrim, Escape, the close button, and Cancel
   *  would all be refused, leaving Remove as the only live control on the confirm view. A
   *  guard whose only exit is the destructive button is a trap, not a guard. What this
   *  refuses is only the accidental dismissals: scrim, Escape, close button, Back. */
  const canClose = !save.isPending && !remove.isPending;

  // Up to ListsPanel's Back guard, whole rather than by term.
  useBackCloseMirror(blockCloseRef, canClose);

  const title =
    view === "picker"
      ? t("lists.addList")
      : view === "confirm"
        ? t("common.removeNamedQuestion", { name: editing?.name })
        : editing
          ? t("common.editNamed", { name: editing.name })
          : t("lists.modal.addTypedTitle", { sourceName: sourceName(source) });

  return (
    <ModalShell title={title} onClose={onClose} canClose={canClose} className="service-modal">
      {view === "picker" && (
        <div className="service-form">
          <div className="pick-group">
            <h3>{t("common.brand.plex")}</h3>
            <div className="pick-grid">
              <PickCard
                name={t("lists.picker.collectionName")}
                blurb={t("lists.picker.collectionBlurb")}
              >
                <div className="acts">
                  {/* Each card's Add is named for its card: three buttons reading "Add" are
                      indistinguishable to anyone hearing them listed. */}
                  <button
                    type="button"
                    className="ghost sm"
                    aria-label={t("lists.picker.addPlexCollectionAria")}
                    onClick={() => openForm("plex_collection")}
                  >
                    {t("common.add")}
                  </button>
                </div>
              </PickCard>
              <PickCard
                name={t("lists.picker.watchlistName")}
                // No second sentence about another user's watchlist: there is no way to sign
                // Reaper into a second Plex account, so naming one would advertise a route
                // that does not exist.
                blurb={t("lists.plexWatchlistDescription")}
              >
                <div className="acts">
                  <button
                    type="button"
                    className="ghost sm"
                    aria-label={t("lists.picker.addPlexWatchlistAria")}
                    onClick={() => openForm("plex_watchlist")}
                  >
                    {t("common.add")}
                  </button>
                </div>
              </PickCard>
            </div>
          </div>
          <div className="pick-group">
            <h3>{t("common.brand.sonarrAndRadarr")}</h3>
            <div className="pick-grid">
              <PickCard name={t("lists.picker.tagsName")} blurb={t("lists.picker.tagsBlurb")}>
                <div className="acts">
                  <button
                    type="button"
                    className="ghost sm"
                    aria-label={t("lists.picker.addTagListAria")}
                    onClick={() => openForm("arr_tag")}
                  >
                    {t("common.add")}
                  </button>
                </div>
              </PickCard>
            </div>
          </div>
          <div className="pick-group">
            <h3>{t("common.brand.imdb")}</h3>
            <div className="pick-grid">
              <PickCard
                name={t("lists.picker.imdbListName")}
                blurb={t("lists.picker.imdbBlurb")}
                cardRef={presetsRef}
              >
                <div className="acts">
                  <button type="button" className="ghost sm" onClick={() => openForm("imdb")}>
                    {t("lists.picker.custom")}
                  </button>
                  {/* The menu is anchored on this wrapper, so it aligns to the button that
                      opens it rather than to the whole row. */}
                  <span className="preset-anchor">
                    {/* The name is "Presets" alone: the arrow is decoration a reader may voice
                        as a shape name, and aria-expanded already says what it draws. */}
                    <button
                      type="button"
                      className="ghost sm"
                      aria-label={t("lists.picker.presetsLabel")}
                      aria-expanded={presetsOpen}
                      aria-controls={presetsOpen ? "imdb-preset-menu" : undefined}
                      onClick={() => setPresetsOpen((v) => !v)}
                    >
                      {t("lists.picker.presetsLabel")} <span aria-hidden="true">▾</span>
                    </button>
                    {presetsOpen && (
                      <FilterMenu
                        id="imdb-preset-menu"
                        label={t("lists.picker.presetsLabel")}
                        anchorClass="preset-anchor"
                      >
                        {IMDB_PRESETS.map((key) => (
                          <li key={key}>
                            <button
                              type="button"
                              className="filter-mi"
                              onClick={() => openPreset(key)}
                            >
                              {presetLabel(key)}
                            </button>
                          </li>
                        ))}
                      </FilterMenu>
                    )}
                  </span>
                </div>
              </PickCard>
            </div>
          </div>
          <div className="add-actions">
            <span className="flex-spacer" />
            <button type="button" className="ghost" onClick={onClose}>
              {t("common.cancel")}
            </button>
          </div>
        </div>
      )}

      {view === "form" && (
        <form
          className="service-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (missing) return;
            save.mutate();
          }}
        >
          <label className="field-sm">
            <span className="field-label">{t("lists.field.name")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("lists.exampleNeverReap")}
              autoFocus={!editing}
              // The same 100 the schema and `_clean_name` bound, so the operator meets the
              // limit as a box that stops taking characters rather than as Pydantic's
              // "String should have at most 100 characters". The schema's bound is checked
              // before the service runs, so the sentence written for this refusal could
              // never fire from typing alone. It is still reachable by paste without this
              // limit, since nothing here typed 101 characters. `test_list_config.py` is
              // where the producer and consumer are held to the same number.
              maxLength={LIST_NAME_MAX}
              aria-describedby={describedBy("name")}
            />
          </label>

          {editing && (
            // Stated, never offered. See the note at the top of this file: the stored
            // membership is keyed on a slug carrying the source, so changing it here would
            // leave the old list enabled and still protecting.
            <p className="help">
              {t("lists.help.sourceLocked", { sourceName: sourceName(editing.source) })}
            </p>
          )}

          {source === "plex_collection" && (
            <>
              <label className="field-sm">
                <span className="field-label">{t("lists.field.plexLibrary")}</span>
                {canPickLibrary ? (
                  <select
                    aria-label={t("lists.field.plexLibrary")}
                    value={library}
                    onChange={(e) => setLibrary(e.target.value)}
                    aria-describedby={describedBy("library")}
                  >
                    <option value="">{t("lists.pickLibraryOption")}</option>
                    {libraryOptions.map((libraryTitle) => (
                      <option key={libraryTitle} value={libraryTitle}>
                        {libraryTitle}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={library}
                    onChange={(e) => setLibrary(e.target.value)}
                    placeholder={t("lists.placeholder.movies")}
                    aria-describedby={describedBy("library")}
                  />
                )}
              </label>
              {!canPickLibrary && (
                <p className="help">
                  {libraries.isPending
                    ? t("lists.help.readingLibraries")
                    : t("lists.help.cantReadLibraries")}
                </p>
              )}
              <label className="field-sm">
                <span className="field-label">{t("lists.field.collection")}</span>
                <input
                  value={collection}
                  onChange={(e) => setCollection(e.target.value)}
                  placeholder={t("lists.exampleNeverReap")}
                  aria-describedby={describedBy("collection")}
                />
              </label>
              <p className="help">{t("lists.help.collectionName")}</p>
            </>
          )}

          {source === "plex_watchlist" && <p className="help">{t("lists.help.watchlistSetup")}</p>}

          {source === "arr_tag" && (
            <div className="field-sm">
              <span className="field-label">{t("lists.field.tags")}</span>
              <TagsEditor
                tags={tags}
                onTags={setTags}
                addLabel={t("common.addTag")}
                describedBy={describedBy("tags")}
              />
              {/* Directly beneath the box it is about, and above the any/all pair, which is a
                  separate question. Placed under the pair instead, it would read as covering
                  both, leaving the pair with no help text of its own. */}
              <p className="help">{t("lists.help.tagsFormat")}</p>
              <Segmented
                value={match}
                options={MATCH_OPTIONS}
                onChange={setMatch}
                variant="flat"
                label={t("lists.matchLabel")}
              />
            </div>
          )}

          {source === "imdb" &&
            (preset ? (
              <p className="help">
                {t("lists.help.presetDescription", { label: presetLabel(preset) })}
              </p>
            ) : (
              <label className="field-sm">
                <span className="field-label">{t("lists.field.imdbIdOrUrl")}</span>
                <input
                  value={imdbId}
                  onChange={(e) => setImdbId(e.target.value)}
                  placeholder={t("lists.placeholder.imdbId")}
                  aria-describedby={describedBy("imdb")}
                />
              </label>
            ))}

          {save.error && (
            <Notice tone="error">
              {t("lists.saveError", { error: describeError(save.error) })}
            </Notice>
          )}

          <div className="add-actions">
            {editing && (
              <button type="button" className="ghost danger" onClick={() => setView("confirm")}>
                {t("lists.removeListEllipsis")}
              </button>
            )}
            <span className="flex-spacer" />
            {/* Live through the save: it is the deliberate way out, and it is what keeps
                `canClose` a guard rather than a trap (see `canClose` above). */}
            <button type="button" className="ghost" onClick={onClose}>
              {t("common.cancel")}
            </button>
            <button type="submit" className="primary" disabled={!!missing || save.isPending}>
              {t("lists.submitButton", {
                state: save.isPending ? "saving" : editing ? "save" : "add",
              })}
            </button>
            {missing && (
              <span className="help help-warn" id={BLOCKED_ID}>
                {missing}
              </span>
            )}
          </div>
        </form>
      )}

      {view === "confirm" && editing && (
        <div className="service-form">
          <p>{t("lists.removeWarning")}</p>
          {remove.error && (
            <Notice tone="error">
              {t("lists.removeError", { error: describeError(remove.error) })}
            </Notice>
          )}
          <div className="add-actions">
            <span className="flex-spacer" />
            {/* Live through the remove, same reason as the form's Cancel: with it disabled,
                the one control still pressable on this view was Remove itself. */}
            <button type="button" className="ghost" onClick={() => setView("form")}>
              {t("common.cancel")}
            </button>
            {/* `danger` alone is the app's destructive button, the one the reap confirmation
                and the restore card use. */}
            <button
              type="button"
              className="danger"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              {t("lists.removeButton", { removing: remove.isPending ? "true" : "false" })}
            </button>
          </div>
        </div>
      )}
    </ModalShell>
  );
}
