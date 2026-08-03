// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Add or edit one protection list (#475). Settings owns what a list IS and where it comes
// from; Policy owns what it does. Nothing is configured in two places.
//
// The source is chosen once, when the list is added, and is fixed afterwards. The stored
// membership is keyed on a slug that carries the source, so re-pointing a Plex collection at
// an *arr tag would leave the old membership enabled under the old slug -- still protecting
// from a definition the operator has already replaced, which is the failure `retire_absent`
// exists to prevent. Editing a list that points at the wrong thing means deleting it and
// adding the right one, which is one more click and cannot strand a protection.
//
// Every refusal rendered here is the server's own sentence. `services.list_config` writes
// them for the operator and names the box that is empty, so re-phrasing them here would be
// one requirement written twice, in two places, drifting from the check that enforces it
// (rule 144).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, type ListConfig, type ListConfigBody } from "../api";
import { ModalShell } from "./ModalShell";
import { Notice } from "./Notice";
import { Segmented } from "./Segmented";
import { Switch } from "./Switch";
import { TagsEditor } from "./TagsEditor";

type Source = ListConfig["source"];

/** What the operator is told each source is, in their words. A curated list is not offered
 *  as a choice -- it ships with Reaper -- but it is named here because the edit form for the
 *  shipped list still has to say what kind of thing it is looking at. */
const SOURCE_NAMES: Record<Source, string> = {
  plex_collection: "Plex collection",
  arr_tag: "Sonarr or Radarr tag",
  curated: "Ships with Reaper",
};

/** The two an operator can make. Order is deliberate: the Plex collection needs no Reaper
 *  concept at all -- you add a film to it from the phone app you already use. */
const ADDABLE: readonly (readonly [Source, string])[] = [
  ["plex_collection", SOURCE_NAMES.plex_collection],
  ["arr_tag", SOURCE_NAMES.arr_tag],
];

export function ListModal({
  editing,
  onClose,
}: {
  editing: ListConfig | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const [name, setName] = useState(editing?.name ?? "");
  const [source, setSource] = useState<Source>(editing?.source ?? "plex_collection");
  const [library, setLibrary] = useState(editing?.config.library ?? "");
  const [collection, setCollection] = useState(editing?.config.collection ?? "");
  const [tags, setTags] = useState<string[]>(editing?.config.tags ?? []);
  const [match, setMatch] = useState<"any" | "all">(editing?.config.match ?? "any");
  const [enabled, setEnabled] = useState(editing?.enabled ?? true);

  // The operator's real Plex libraries, so the one field that made an install unable to reap
  // at all is picked rather than typed (#483: the keep collection was read out of a library
  // hardcoded to "Movies", so a library named anything else was never read). Optional and
  // soft -- see `libraryOptions` for what happens when Plex cannot be asked.
  const libraries = useQuery({
    queryKey: ["plex-libraries"],
    queryFn: api.plexLibraries,
    enabled: source === "plex_collection",
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

  const body = (): ListConfigBody =>
    source === "plex_collection" ? { library, collection } : { tags, match };

  const save = useMutation({
    mutationFn: async () => {
      if (editing) {
        // `config` is omitted for the shipped list: its body says which curated list it is,
        // and that is not the operator's to retype (rule 1 -- omitted means "leave it").
        return api.editList(editing.id, {
          name,
          enabled,
          ...(editing.source === "curated" ? {} : { config: body() }),
        });
      }
      return api.addList(name, source, body());
    },
    onSuccess: async () => {
      // Both halves: the definitions this modal wrote, and the health rows keyed on them --
      // a list switched off leaves the health view in the same flush, rather than sitting
      // there reading "Working" against a definition that no longer syncs (rule 79).
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["lists-configured"] }),
        queryClient.invalidateQueries({ queryKey: ["lists"] }),
      ]);
      onClose();
    },
  });

  const curated = editing?.source === "curated";
  // What the submit button is waiting on. The server refuses each of these too, in the same
  // words; saying it here means the operator reads it while looking at the empty box rather
  // than after a round trip (the shape `_clean_config`'s docstring describes).
  const missing = ((): string | null => {
    if (!name.trim()) return "Give the list a name, so you can pick it out on the Policy screen.";
    if (curated) return null;
    if (source === "plex_collection") {
      if (!library.trim()) return "Say which Plex library to look in.";
      if (!collection.trim()) return "Say which collection in that library to read.";
      return null;
    }
    return tags.length === 0
      ? "Add at least one tag, spelled as it appears in Sonarr or Radarr."
      : null;
  })();

  return (
    <ModalShell
      title={editing ? `Edit ${editing.name}` : "Add a list"}
      onClose={onClose}
      // A close mid-save unmounts the only place the refusal is ever shown, and the operator
      // walks away believing it saved. Cancel below stays live throughout, so this guard
      // never becomes the trap rule 146 describes.
      canClose={!save.isPending}
      className="service-modal"
    >
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
          />
        </label>

        {editing ? (
          // Stated, never offered. See the note at the top of this file: the stored
          // membership is keyed on a slug carrying the source, so changing it here would
          // leave the old list enabled and still protecting.
          <p className="help">
            Where it comes from: {SOURCE_NAMES[editing.source]}. To point a list somewhere else,
            remove it and add the one you want.
          </p>
        ) : (
          <div className="field-sm">
            <span className="field-label">Where it comes from</span>
            <Segmented
              value={source}
              onChange={setSource}
              label="Where the list comes from"
              options={ADDABLE}
              fill
            />
          </div>
        )}

        {!curated && source === "plex_collection" && (
          <>
            <label className="field-sm">
              <span className="field-label">Plex library</span>
              {canPickLibrary ? (
                <select
                  aria-label="Plex library"
                  value={library}
                  onChange={(e) => setLibrary(e.target.value)}
                >
                  <option value="">Pick a library…</option>
                  {libraryOptions.map((title) => (
                    <option key={title} value={title}>
                      {title}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  value={library}
                  onChange={(e) => setLibrary(e.target.value)}
                  placeholder="Movies"
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
              />
            </label>
            <p className="help">
              The collection's name in Plex. Add a title to it from the Plex app and it is kept from
              the next scan on.
            </p>
          </>
        )}

        {!curated && source === "arr_tag" && (
          <div className="field-sm">
            <span className="field-label">Tags</span>
            <TagsEditor
              tags={tags}
              match={match}
              onTags={setTags}
              onMatch={setMatch}
              addLabel="Add a tag"
              matchLead="Put a title on this list if it has"
              emptyHelp="No tags: this list would hold nothing."
            />
            <p className="help">
              Type each tag exactly as it appears in Sonarr or Radarr. Every connected server is
              read, so a tag used on more than one is one list here.
            </p>
          </div>
        )}

        {curated && (
          <p className="help">
            This list ships with Reaper and keeps itself up to date. You can rename it or switch it
            off; it cannot be pointed somewhere else or removed.
          </p>
        )}

        {editing && (
          <label className="toggle">
            <Switch checked={enabled} onChange={setEnabled} />
            <span>Protecting</span>
          </label>
        )}
        {editing && !enabled && (
          <Notice tone="warn">
            Switched off, nothing on this list is protected. Anything it was keeping can be deleted
            by the next scan.
          </Notice>
        )}

        {save.error && <Notice tone="error">The list wasn't saved: {save.error.message}</Notice>}

        <div className="add-actions">
          <span className="flex-spacer" />
          <button type="button" className="ghost" onClick={onClose} disabled={save.isPending}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={!!missing || save.isPending}>
            {save.isPending ? "Saving…" : editing ? "Save" : "Add list"}
          </button>
          {missing && (
            <span className="help help-warn" id="list-modal-blocked">
              {missing}
            </span>
          )}
        </div>
      </form>
    </ModalShell>
  );
}
