// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A set of *arr tags, as removable chips plus a free-type box, with the ANY/ALL switch that
// says how many of them a title needs.
//
// It lives here rather than inside `PolicyEditor` because two screens now ask for the same
// thing: the policy's "Spare titles you've tagged" card, and a tag list the operator defines
// on Settings -> Lists. The tags are typed by hand against a spelling only Sonarr and Radarr
// know, so the chip-and-remove affordance, the comma handling and the focus recovery below
// are the whole usability of the control -- a second copy of it on the newer screen would be
// a second set of those to get right (rule 18).

import { useRef, useState, type RefObject } from "react";

import { REMOVES_ITS_ROW, useRemovalFocus } from "../focus";
import { Segmented } from "./Segmented";

export function TagsEditor({
  tags,
  match,
  onTags,
  onMatch,
  /** What the ANY/ALL line leads with, and the accessible name of the add box. The two
   *  callers phrase the same control differently: the policy card is talking about titles
   *  being kept, and the list modal about what goes ON the list. */
  addLabel = "Add a keep tag",
  matchLead = "Keep a title with",
  /** The sentence under an empty set. Rule 53's shape: a protection configured with nothing
   *  in it protects nothing, and the control says so where it is, rather than leaving the
   *  operator to find out from a "Nothing on it" row later. */
  emptyHelp = "No tags: this protection keeps nothing.",
}: {
  tags: string[];
  match: "any" | "all";
  onTags: (t: string[]) => void;
  onMatch: (m: "any" | "all") => void;
  addLabel?: string;
  matchLead?: string;
  emptyHelp?: string;
}) {
  const [input, setInput] = useState("");
  const add = () => {
    const t = input.trim();
    if (t && !tags.includes(t)) onTags([...tags, t]);
    setInput("");
  };
  // Removing a chip destroys the button holding focus, so without this the operator lands on
  // `<body>` and the next Tab restarts above the whole ~1,900-line policy form -- three times
  // over for three tags (#173). Focus goes to the next chip's ✕, or to the add box once the
  // last one is gone.
  const addRef = useRef<HTMLInputElement>(null);
  const chips = useRemovalFocus(addRef);
  return (
    <div className="keep-tags">
      <div className="tag-chips" ref={chips.ref as RefObject<HTMLDivElement>}>
        {tags.map((t, i) => (
          <span key={t} className="tag-chip">
            {t}
            <button
              {...REMOVES_ITS_ROW}
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
      {tags.length >= 1 && (
        <div className="tag-match">
          <span className="muted">{matchLead}</span>
          <Segmented
            value={match}
            onChange={onMatch}
            label="How many of these tags a title needs"
            options={[
              ["any", "any of these tags"],
              ["all", "all of these tags"],
            ]}
          />
        </div>
      )}
      {tags.length === 0 && <p className="help">{emptyHelp}</p>}
    </div>
  );
}
