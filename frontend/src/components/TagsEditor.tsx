// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A set of *arr tags, as removable chips plus a free-type box.
//
// One consumer today (the list modal's tag form), and still its own file: the tags are typed
// by hand against a spelling only Sonarr and Radarr know, so the chip-and-remove affordance,
// the comma handling and the focus recovery below are the whole usability of the control, and
// a second copy would be a second set of those to get right (rule 18). How many of the tags a
// title needs is the caller's control, rendered beside this one -- it is a fact about the
// list, not about the chips.

import { useRef, useState, type RefObject } from "react";

import { REMOVES_ITS_ROW, useRemovalFocus } from "../focus";

export function TagsEditor({
  tags,
  onTags,
  /** The accessible name of the add box. */
  addLabel = "Add a tag",
}: {
  tags: string[];
  onTags: (t: string[]) => void;
  addLabel?: string;
}) {
  const [input, setInput] = useState("");
  const add = () => {
    const t = input.trim();
    if (t && !tags.includes(t)) onTags([...tags, t]);
    setInput("");
  };
  // Removing a chip destroys the button holding focus, so without this the operator lands on
  // `<body>` and the next Tab restarts at the top of the form -- once per removed tag (#173).
  // Focus goes to the next chip's ✕, or to the add box once the last one is gone.
  const addRef = useRef<HTMLInputElement>(null);
  const chips = useRemovalFocus(addRef);
  return (
    <div className="tag-editor">
      <div className="tag-chips" ref={chips.ref as RefObject<HTMLDivElement>}>
        {tags.map((t, i) => (
          <span key={t} className="tag-chip">
            {t}
            <button
              {...REMOVES_ITS_ROW}
              // Inside the list modal's <form>, where a bare <button> is type="submit" and a
              // chip's ✕ would otherwise save the list it was trimming.
              type="button"
              onClick={() => {
                chips.removing(i);
                onTags(tags.filter((x) => x !== t));
              }}
              aria-label={`Remove ${t}`}
            >
              ✕
            </button>
          </span>
        ))}
        <input
          ref={addRef}
          // A placeholder is a name of last resort, so this box was announcing itself as the
          // example text inside it and lost even that the moment anything was typed. Same
          // defect #136 fixed on the Plex panel's address pair.
          aria-label={addLabel}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            }
          }}
          onBlur={add}
          placeholder="add a tag…"
        />
      </div>
    </div>
  );
}
