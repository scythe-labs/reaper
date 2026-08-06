// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Add or edit one protection list. Settings owns what a list IS and where it comes from;
// Policy owns what it does, through a keep rule naming the list.
//
// Adding walks two steps, the way the *arrs add an import list: a type picker (Plex
// collection, Plex watchlist, Sonarr and Radarr tags, IMDb), then the one form that type
// needs. The source is chosen once and is fixed afterwards: the stored membership is keyed
// on a slug that carries the source, so re-pointing a Plex collection at an *arr tag would
// leave the old membership enabled under the old slug -- still protecting from a definition
// the operator has already replaced, which is the failure `retire_absent` exists to prevent.
//
// Removing lives INSIDE Edit, as the third view of this one modal, so a row's actions stay
// two buttons and the destructive one sits behind the form that names what it destroys.
//
// The `blocked` sentences below are a SECOND copy of refusals `services.list_config` also
// writes, said here so the operator reads them while looking at the empty box rather than
// after a round trip. Two copies of one requirement is rule 144's hazard, and this file used
// to carry a comment claiming there was only one, which is worse than the duplication: each
// side was pinned by its own test, nothing bound the pair, and a one-sided edit left both
// suites green. `tests/test_list_config.py` names this file in the failure message that
// fires when the server's wording moves, so the copies cannot drift silently.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type ListConfig, type ListConfigBody } from "../api";
import { useBackGuard } from "../backnav";
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
 *  both hold. Exported so the pairing test can read it rather than transcribe it: the
 *  schema's bound is checked first, so `_clean_name`'s "That name is too long" only ever
 *  reaches an operator who got past this box (rules 131, 144). */
export const LIST_NAME_MAX = 100;

/** What the operator is told each source is, in their words. */
const SOURCE_NAMES: Record<Source, string> = {
  plex_collection: "Plex collection",
  plex_watchlist: "Plex watchlist",
  arr_tag: "Sonarr and Radarr tags",
  imdb: "IMDb list",
};

/** The shipped IMDb charts. The keys are the server's (`services.lists.IMDB_PRESETS`); no
 *  route serves them, so this is the one browser copy and it is checked by the tests that
 *  post each key. A stored preset this table does not know still renders, by its raw key. */
const IMDB_PRESETS: readonly { key: string; label: string }[] = [
  { key: "top250", label: "IMDb Top 250" },
  { key: "popular", label: "IMDb Popular Movies" },
];

function presetLabel(key: string): string {
  return IMDB_PRESETS.find((p) => p.key === key)?.label ?? key;
}

/** The any/all pair, there from the start so the form never has a blank where a control
 *  belongs. The shared `Segmented` in its flat variant: the mockup's chrome, one either-or
 *  control (rules 18, 41). */
const MATCH_OPTIONS = [
  ["any", "Any of these"],
  ["all", "All of these"],
] as const satisfies readonly (readonly ["any" | "all", string])[];

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
   *  `rescore` says whether the change moved what a KEEP RULE protects, which is the only kind
   *  of change the queue's stored fates were scored under: true when a rule names the list
   *  (an edit or a remove of a used list), false for a list nothing uses -- every add, since an
   *  added list carries no rule. The panel starts a scan only when it is true, so adding a list
   *  the operator has not wired to Policy does not kick off a full library scan for nothing.
   *
   *  The panel does the acting, for the same reason the check runs there: this modal is
   *  unmounting, and a mutation started on the way out loses the surface that would report
   *  it. */
  onChanged?: ((rescore: boolean) => void) | undefined;
  /** The panel's mirror of `canClose`, so its Back guard refuses exactly what the scrim,
   *  Escape and the ✕ refuse rather than a stale copy of one of the reasons (rule 80). */
  blockCloseRef?: React.MutableRefObject<boolean> | undefined;
}) {
  const queryClient = useQueryClient();

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
  // it the press skipped the menu, was spent on the Settings section frame, and the panel
  // behind the modal navigated while the menu was still drawn (rules 80, 72).
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
    // The watchlist form has nothing to set up, so the name is the one box -- give it a
    // starting value the operator can keep.
    setName(next === "plex_watchlist" ? "My watchlist" : "");
    setView("form");
  };
  const openPreset = (key: string) => {
    setSource("imdb");
    setPreset(key);
    setName(presetLabel(key));
    setView("form");
  };

  // The operator's real Plex libraries, so the one field that made an install unable to reap
  // at all is picked rather than typed (#483: the keep collection was read out of a library
  // hardcoded to "Movies", so a library named anything else was never read). Optional and
  // soft -- see `libraryOptions` for what happens when Plex cannot be asked.
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
   *  `ListConfigPatch` is omitted-means-keep and `list_config.update` replaces only the
   *  fields it is given, so a save that sends `config` at all writes the whole thing back.
   *  This form seeds ONCE, from a `lists-configured` row the cache may have held for a
   *  while: `main.tsx` sets `refetchOnWindowFocus: false` with a 30 second `staleTime`, and
   *  nothing refetches between the panel rendering and this modal opening. So a rename typed
   *  against a stale row silently reverted a collection someone else had repointed, or tags
   *  they had changed, in another tab (the `cached-value-drives-a-write` shape of #203/#204).
   *
   *  Compared field by field against the same expressions that seeded each piece of state,
   *  which is what makes these the canonical forms rule 39 asks for: comparing the whole
   *  stored `config` instead would read a defaulted `match` as an edit on every save. */
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
      // the policies -- adding a list writes no rule, but a rename re-spells every rule
      // naming it, so a stale policy cache would render rules about a list name that no
      // longer exists (rule 79).
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["lists-configured"] }),
        queryClient.invalidateQueries({ queryKey: ["lists"] }),
        queryClient.invalidateQueries({ queryKey: ["policy"] }),
      ]);
      // After the refetch, so the row this names is already on screen to report the check.
      // The server's row, not the form's fields: it carries the id, and it is the cleaned
      // copy the save actually stored (rule 39).
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
      // Removing a list that a rule named drops that protection, so the queue must re-score;
      // removing one nothing used changes no fate.
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
      return {
        on: "name",
        why: "Give the list a name, so you can pick it out on the Policy screen.",
      };
    }
    if (source === "plex_collection") {
      if (!library.trim()) return { on: "library", why: "Say which Plex library to look in." };
      if (!collection.trim()) {
        return { on: "collection", why: "Say which collection in that library to read." };
      }
      return null;
    }
    if (source === "arr_tag") {
      return tags.length === 0
        ? { on: "tags", why: "Add at least one tag, spelled as it appears in Sonarr or Radarr." }
        : null;
    }
    if (source === "imdb" && !preset && !imdbId.trim()) {
      return {
        on: "imdb",
        why: "Paste the list's id or URL. An IMDb list id looks like ls000000000.",
      };
    }
    return null;
  })();
  const missing = blocked?.why ?? null;
  /** `aria-describedby` for the one control the blocking sentence is about. */
  const describedBy = (field: BlockedField) => (blocked?.on === field ? BLOCKED_ID : undefined);

  /** Whether a dismissal is allowed, computed ONCE and handed to every path that can dismiss.
   *
   *  A close mid-save unmounts the only place the refusal is ever shown -- the scrim swallows
   *  the server's sentence, the invalidations never run, and the operator walks away believing
   *  the list saved.
   *
   *  Cancel is deliberately NOT gated on this, the arrangement `ServiceModal` states and this
   *  modal reversed: both Cancels were disabled while their mutation was in flight, so in the
   *  one state this guard refuses a close, the scrim, Escape, the ✕ AND Cancel were all
   *  refused and the only live control on the confirm view was Remove. A guard whose only exit
   *  is the destructive button is a trap, not a guard (rule 146). What this refuses is the
   *  ACCIDENTAL dismissals: scrim, Escape, ✕, Back. */
  const canClose = !save.isPending && !remove.isPending;

  // Mirror it up to ListsPanel's Back guard, so browser Back honors the same predicate the
  // scrim, Escape and the ✕ do (rule 80), and clear it on unmount so a stale true never
  // lingers after the modal closes.
  useEffect(() => {
    if (blockCloseRef) blockCloseRef.current = !canClose;
    return () => {
      if (blockCloseRef) blockCloseRef.current = false;
    };
  }, [canClose, blockCloseRef]);

  const title =
    view === "picker"
      ? "Add a list"
      : view === "confirm"
        ? `Remove ${editing?.name}?`
        : editing
          ? `Edit ${editing.name}`
          : `Add a list: ${SOURCE_NAMES[source]}`;

  return (
    <ModalShell title={title} onClose={onClose} canClose={canClose} className="service-modal">
      {view === "picker" && (
        <div className="service-form">
          <div className="pick-group">
            <h3>Plex</h3>
            <div className="pick-grid">
              <PickCard
                name="Collection"
                blurb="A collection you curate in the Plex app. Add a title from your phone and it's covered."
              >
                <div className="acts">
                  {/* Each card's Add is named for its card: three buttons reading "Add" are
                      indistinguishable to anyone hearing them listed. */}
                  <button
                    type="button"
                    className="ghost sm"
                    aria-label="Add a Plex collection"
                    onClick={() => openForm("plex_collection")}
                  >
                    Add
                  </button>
                </div>
              </PickCard>
              <PickCard
                name="Watchlist"
                // No second sentence about another user's watchlist: there is no way to sign
                // Reaper into a second Plex account, so naming one advertised a route that
                // does not exist (rule 25).
                blurb="The watchlist of the Plex account Reaper is signed in with."
              >
                <div className="acts">
                  <button
                    type="button"
                    className="ghost sm"
                    aria-label="Add a Plex watchlist"
                    onClick={() => openForm("plex_watchlist")}
                  >
                    Add
                  </button>
                </div>
              </PickCard>
            </div>
          </div>
          <div className="pick-group">
            <h3>Sonarr and Radarr</h3>
            <div className="pick-grid">
              <PickCard
                name="Tags"
                blurb="Titles carrying tags you pick, read from every connected server."
              >
                <div className="acts">
                  <button
                    type="button"
                    className="ghost sm"
                    aria-label="Add a tag list"
                    onClick={() => openForm("arr_tag")}
                  >
                    Add
                  </button>
                </div>
              </PickCard>
            </div>
          </div>
          <div className="pick-group">
            <h3>IMDb</h3>
            <div className="pick-grid">
              <PickCard
                name="IMDb list"
                blurb="A public IMDb list or chart, refreshed on its own."
                cardRef={presetsRef}
              >
                <div className="acts">
                  <button type="button" className="ghost sm" onClick={() => openForm("imdb")}>
                    Custom
                  </button>
                  {/* The menu is anchored on this wrapper, so it aligns to the button that
                      opens it rather than to the whole row (rule 138). */}
                  <span className="preset-anchor">
                    {/* The name is "Presets" alone: the arrow is decoration a reader may voice
                        as a shape name, and aria-expanded already says what it draws (rule 21). */}
                    <button
                      type="button"
                      className="ghost sm"
                      aria-label="Presets"
                      aria-expanded={presetsOpen}
                      aria-controls={presetsOpen ? "imdb-preset-menu" : undefined}
                      onClick={() => setPresetsOpen((v) => !v)}
                    >
                      Presets <span aria-hidden="true">▾</span>
                    </button>
                    {presetsOpen && (
                      <FilterMenu id="imdb-preset-menu" label="Presets" anchorClass="preset-anchor">
                        {IMDB_PRESETS.map((p) => (
                          <li key={p.key}>
                            <button
                              type="button"
                              className="filter-mi"
                              onClick={() => openPreset(p.key)}
                            >
                              {p.label}
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
              Cancel
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
            <span className="field-label">Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Never Reap"
              autoFocus={!editing}
              // The same 100 the schema and `_clean_name` bound, so the operator meets the
              // limit as a box that stops taking characters rather than as Pydantic's
              // "String should have at most 100 characters" (rule 21): the schema's bound is
              // checked before the service runs, so the sentence written for this refusal
              // could never fire. Reachable by paste without it, since nothing here typed
              // 101 characters (rule 131: producer and consumer read one number, and
              // `test_list_config.py` is where the two are held together).
              maxLength={LIST_NAME_MAX}
              aria-describedby={describedBy("name")}
            />
          </label>

          {editing && (
            // Stated, never offered. See the note at the top of this file: the stored
            // membership is keyed on a slug carrying the source, so changing it here would
            // leave the old list enabled and still protecting.
            <p className="help">
              Where it comes from: {SOURCE_NAMES[editing.source]}. To point a list somewhere else,
              remove it and add the one you want.
            </p>
          )}

          {source === "plex_collection" && (
            <>
              <label className="field-sm">
                <span className="field-label">Plex library</span>
                {canPickLibrary ? (
                  <select
                    aria-label="Plex library"
                    value={library}
                    onChange={(e) => setLibrary(e.target.value)}
                    aria-describedby={describedBy("library")}
                  >
                    <option value="">Pick a library…</option>
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
                    placeholder="Movies"
                    aria-describedby={describedBy("library")}
                  />
                )}
              </label>
              {!canPickLibrary && (
                <p className="help">
                  {libraries.isPending
                    ? "Reading your Plex libraries…"
                    : "Reaper couldn't read your Plex libraries, so type the name exactly as it " +
                      "appears in Plex. A name that doesn't match means this list protects nothing."}
                </p>
              )}
              <label className="field-sm">
                <span className="field-label">Collection</span>
                <input
                  value={collection}
                  onChange={(e) => setCollection(e.target.value)}
                  placeholder="Never Reap"
                  aria-describedby={describedBy("collection")}
                />
              </label>
              <p className="help">
                The collection's name in Plex. Add a title to it from the Plex app and it is kept
                from the next scan on.
              </p>
            </>
          )}

          {source === "plex_watchlist" && (
            <p className="help">
              Reaper reads the watchlist of the Plex account it is signed in with. Nothing else to
              set up.
            </p>
          )}

          {source === "arr_tag" && (
            <div className="field-sm">
              <span className="field-label">Tags</span>
              <TagsEditor
                tags={tags}
                onTags={setTags}
                addLabel="Add a tag"
                describedBy={describedBy("tags")}
              />
              {/* Directly beneath the box it is about, and above the any/all pair, which is a
                  separate question. It sat under the pair, so it read as covering both and the
                  pair got none of its own (rule 45). */}
              <p className="help">
                Type each tag exactly as it appears in Sonarr or Radarr. Every connected server is
                read.
              </p>
              <Segmented
                value={match}
                options={MATCH_OPTIONS}
                onChange={setMatch}
                variant="flat"
                label="How many of these tags a title needs"
              />
            </div>
          )}

          {source === "imdb" &&
            (preset ? (
              <p className="help">
                The {presetLabel(preset)} preset. Refreshed on its own, no sign-in needed.
              </p>
            ) : (
              <label className="field-sm">
                <span className="field-label">List id or URL</span>
                <input
                  value={imdbId}
                  onChange={(e) => setImdbId(e.target.value)}
                  placeholder="ls000000000, or paste the list's URL"
                  aria-describedby={describedBy("imdb")}
                />
              </label>
            ))}

          {save.error && <Notice tone="error">The list wasn't saved: {save.error.message}</Notice>}

          <div className="add-actions">
            {editing && (
              <button type="button" className="ghost danger" onClick={() => setView("confirm")}>
                Remove list…
              </button>
            )}
            <span className="flex-spacer" />
            {/* Live through the save: it is the deliberate way out, and it is what keeps
                `canClose` a guard rather than a trap (see `canClose` above, rule 146). */}
            <button type="button" className="ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={!!missing || save.isPending}>
              {save.isPending ? "Saving…" : editing ? "Save" : "Add list"}
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
          <p>
            Anything this list was keeping can be deleted by the next scan. Its keep rules on Policy
            go with it. Reaper does not delete the collection or the tags themselves, only its own
            record of them.
          </p>
          {remove.error && (
            <Notice tone="error">The list wasn't removed: {remove.error.message}</Notice>
          )}
          <div className="add-actions">
            <span className="flex-spacer" />
            {/* Live through the remove, same reason as the form's Cancel: with it disabled,
                the one control still pressable on this view was Remove itself. */}
            <button type="button" className="ghost" onClick={() => setView("form")}>
              Cancel
            </button>
            {/* `danger` alone is the app's destructive button, the one the reap confirmation
                and the restore card use (rule 18). */}
            <button
              type="button"
              className="danger"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              {remove.isPending ? "Removing…" : "Remove list"}
            </button>
          </div>
        </div>
      )}
    </ModalShell>
  );
}
