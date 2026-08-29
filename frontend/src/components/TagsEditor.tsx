// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A set of *arr tags, as removable chips plus a free-type box.
//
// One consumer today (the list modal's tag form), and still its own file: the tags are
// typed by hand against a spelling only Sonarr and Radarr know, so the chip-and-remove
// affordance, the comma handling and the focus recovery below are the whole usability of
// the control, and a second copy would be a second set of those to get right. How many of
// the tags a title needs is the caller's control, rendered beside this one: it is a fact
// about the list, not about the chips.

import { useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import { REMOVES_ITS_ROW, useRemovalFocus } from "../focus";

export function TagsEditor({
  tags,
  onTags,
  /** The accessible name of the add box. */
  addLabel,
  /** The id of a sentence saying why the form will not submit. It lands on the add box,
   *  because the disabled submit that sentence is about is out of the Tab order. */
  describedBy,
}: {
  tags: string[];
  onTags: (t: string[]) => void;
  addLabel?: string;
  describedBy?: string | undefined;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const add = () => {
    const tag = input.trim();
    // Case-folded, because Sonarr and Radarr lower-case every label: "Keep" and "keep" are
    // one tag there, and adding both here would leave the list reporting a count of zero
    // against whichever spelling lost. The server folds the same way on save.
    const already = tags.some((x) => x.toLowerCase() === tag.toLowerCase());
    if (tag && !already) onTags([...tags, tag]);
    setInput("");
  };
  // Removing a chip destroys the button holding focus, so without this the operator lands
  // on `<body>` and the next Tab restarts at the top of the form, once per removed tag.
  // Focus goes to the next chip's ✕, or to the add box once the last one is gone.
  const addRef = useRef<HTMLInputElement>(null);
  const chips = useRemovalFocus(addRef);
  return (
    <div className="tag-editor">
      <div className="tag-chips" ref={chips.ref as RefObject<HTMLDivElement>}>
        {tags.map((tag, i) => (
          <span key={tag} className="tag-chip">
            {tag}
            <button
              {...REMOVES_ITS_ROW}
              // Inside the list modal's <form>, where a bare <button> is type="submit" and a
              // chip's ✕ would otherwise save the list it was trimming.
              type="button"
              onClick={() => {
                chips.removing(i);
                onTags(tags.filter((x) => x !== tag));
              }}
              aria-label={t("shell.tagsEditor.removeTag", { tag })}
            >
              ✕
            </button>
          </span>
        ))}
        <input
          ref={addRef}
          // A placeholder is a name of last resort. Without this label, the box would
          // announce itself only as the example text inside it, and lose even that the
          // moment anything was typed. The Plex panel's address pair had the same defect
          // and needed the same fix.
          aria-label={addLabel ?? t("common.addTag")}
          aria-describedby={describedBy}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            }
          }}
          onBlur={add}
          placeholder={t("shell.tagsEditor.placeholder")}
        />
      </div>
    </div>
  );
}
