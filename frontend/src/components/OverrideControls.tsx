// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Spare / Reap pair an operator presses to overrule the scan, wherever it appears: a movie
// card, a season row, the why panel, the show panel, the bulk bar.
//
// This is the control grammar rules 46, 48 and 50 are written in. It used to sit in the middle
// of ReviewQueue.tsx, so the panels that draw it imported their safety controls out of a page
// component, and a reviewer of a two-line rule change read three thousand lines to find them
// (R-1). Nothing moved but the file.
//
// The rules it carries, in short:
//   - the buttons reveal on hover and a decided row rests as its icon (46, OverrideMark);
//   - Reap is dropped wherever the item is ALREADY condemned, judged by the item's own verdict
//     (48; the caller passes `hideReap`, from reviewFate's reapIsNoop / showReapIsNoop);
//   - a control reflects and clears its OWN level, never an inherited decision it cannot undo
//     (50, `override_own`), and KeptByShowNote says so beside a season that a whole-show
//     decision is keeping or reaping.

import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import type { Override } from "../api";
import { useBackGuard } from "../backnav";
import { spareRemaining } from "../format";
import { CaretDownGlyph, ClockGlyph, PenGlyph, ScytheIcon, SpareGlyph } from "./queueIcons";
import { useDefaultSpareDays } from "./queueSettings";

//: The quick day-lengths the Spare menu always offers, above Forever and a Custom entry. The
//  operator's own default is added to (and tagged in) this list when it is a different number.
export const SPARE_PRESETS = [30, 90];

/** The hand-overrides, as a toggle: **Spare** (keep) and **Reap** (force onto the list). The
 *  active one is lit; clicking it again clears the override and lets Reaper judge the item
 *  again. Clicking the other switches. Stops the click from opening the panel.
 *
 *  Spare is a SPLIT button: the main press keeps the item for the operator's default length
 *  (Settings → General), and the chevron opens the other lengths -- a set number of days, or
 *  forever -- so a one-off choice is one click, never a settings trip. The leading glyph (∞ or
 *  a clock) says which the default is. On a spared item the chevron stays live, so the same
 *  menu extends or shortens the spare; the main button still toggles it off.
 *
 *  On the Condemned lane the item is already on the block, so Reap would change nothing:
 *  `hideReap` drops it there and leaves Spare (rescue) on its own. */
/** Where the Spare length menu is pinned (fixed, viewport coords). Always `left`, plus exactly
 *  one vertical anchor: `top` when it opens below the caret, `bottom` when it flips above -- the
 *  bottom anchor keeps it snug to the caret whatever the menu's rendered height. */
export type MenuPos = { left: number; top?: number; bottom?: number };

export function OverrideControls({
  override,
  onSet,
  onClear,
  pending,
  hideReap = false,
  spareExpiresAt = null,
  roomy = false,
}: {
  override: Override | null;
  onSet: (decision: Override, spareDays?: number) => void;
  onClear: () => void;
  pending: boolean;
  hideReap?: boolean;
  /** When the spare on THIS control's own level runs out (ISO), or null for a forever spare.
   *  Read only while `override` is `"spare"`, which is what makes the effective
   *  `spare_expires_at` safe to pass from a season row: a season with no decision of its own
   *  carries its SHOW's expiry there, but its `override_own` is null, so the button is not in
   *  the spared state and never reads it (rule 50). Once `override_own` IS set, the effective
   *  expiry is that item's own (`effective_spare_expiry` prefers the item's key). */
  spareExpiresAt?: string | null;
  /** The why/show panel footers give the pair the whole row, so the spared button spells the
   *  count out ("Spared 87d"). The card and season-row tracks are fixed at `--ov-btn-w` (rule
   *  51), where only the count fits and the solid green fill carries "you chose this". */
  roomy?: boolean;
}) {
  const defaultDays = useDefaultSpareDays();
  const [menuAt, setMenuAt] = useState<MenuPos | null>(null);
  const caretRef = useRef<HTMLButtonElement>(null);
  // Back closes the open length menu before it does anything else (only the one open menu has a
  // non-null position, so only it registers).
  useBackGuard(menuAt !== null, () => setMenuAt(null));

  // The menu is position:fixed so the card's overflow:hidden can't clip it. Anchor it to the
  // chevron, right-aligned, and flip above when it would run off the bottom of the viewport.
  // Below: pin the menu's TOP under the caret. Above: pin its BOTTOM just over the caret with the
  // `bottom` property -- never a `top` computed from a guessed height, which floated the menu off
  // the button when the real menu was shorter than the guess. HEIGHT is only the flip decision's
  // upper bound now, not a coordinate.
  const toggleMenu = (e: ReactMouseEvent) => {
    e.stopPropagation();
    if (menuAt) {
      setMenuAt(null);
      return;
    }
    const btn = caretRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const WIDTH = 224;
    const HEIGHT = 250;
    const left = Math.max(8, Math.min(r.right - WIDTH, window.innerWidth - WIDTH - 8));
    const below = r.bottom + HEIGHT <= window.innerHeight;
    setMenuAt(
      below ? { left, top: r.bottom + 4 } : { left, bottom: window.innerHeight - r.top + 4 },
    );
  };

  // Undecided, the button says what a press WILL do (the default length). Spared, it says what
  // is in force on this item -- its own glyph, its own count -- because the default stops
  // mattering the moment there is a real answer. Sparing for 90 days under a Forever default
  // used to leave the button reading "∞ Spared", wrong on both counts.
  //
  // Three spared states, never two. An EXPIRED spare is its own: the item is genuinely still
  // kept (the planner, the ledger and the executor all read every spare on file -- only a scan
  // realizes the expiry), so the button must not go dark as though policy had it back. But it
  // is no longer a live decision either, so it must not wear the solid fill that means "you
  // chose this and it holds". It says so instead, and the dashed `.expired` fill below carries
  // the rest.
  const spared = override === "spare";
  const remaining = spareRemaining(spareExpiresAt);
  const counting = spared && !remaining.forever && !remaining.expired;
  const expired = spared && remaining.expired;
  const spareLabel = !spared
    ? "Spare"
    : expired
      ? roomy
        ? "Spare expired"
        : "Expired"
      : counting
        ? roomy
          ? `Spared ${remaining.short}`
          : remaining.short
        : "Spared";

  const clickSpare = (e: ReactMouseEvent) => {
    e.stopPropagation();
    override === "spare" ? onClear() : onSet("spare", defaultDays);
  };
  const clickReap = (e: ReactMouseEvent) => {
    e.stopPropagation();
    override === "reap" ? onClear() : onSet("reap");
  };

  return (
    <div
      className={`override-controls ${menuAt ? "menu-open" : ""}`}
      role="group"
      aria-label="Spare or reap this item"
      // The buttons activate on Enter/Space natively; stop the key from bubbling to a row or
      // card whose own handler calls preventDefault, which would cancel the button's
      // activation and open the panel instead (B-7). Mirrors the SeasonStrip square guard.
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") e.stopPropagation();
      }}
    >
      <span className="ov-split">
        <button
          type="button"
          className={`ov-btn ov-spare split-main ${override === "spare" ? "active" : ""} ${
            expired ? "expired" : ""
          }`}
          disabled={pending}
          aria-pressed={override === "spare"}
          // On the narrow surfaces the visible label is abbreviated to fit the fixed track, so
          // name the button in full for a screen reader. The visible text ("87d", "Expired")
          // stays a substring of that name, which is what WCAG 2.5.3 (Label in Name) asks for.
          aria-label={
            roomy
              ? undefined
              : expired
                ? "Spared, expired"
                : counting
                  ? `Spared ${remaining.short} left`
                  : undefined
          }
          onClick={clickSpare}
          title={
            spared
              ? expired
                ? remaining.note
                : counting
                  ? `${remaining.until}. Click to let Reaper judge it again`
                  : "Spared. Click to let Reaper judge it again"
              : defaultDays > 0
                ? `Spare for ${defaultDays} days. Use the arrow for another length`
                : "Spare forever. Use the arrow for a set time"
          }
        >
          {/* `days` only picks the glyph's shape here: ∞ for a forever spare, the clock for a
              timed one. The count itself rides in the label beside it. */}
          <SpareGlyph days={spared ? (remaining.forever ? 0 : 1) : defaultDays} />{" "}
          <span className="ov-label">{spareLabel}</span>
        </button>
        <button
          ref={caretRef}
          type="button"
          // The caret takes `.expired` too, so the pair reads as one dashed control rather than
          // a dashed half joined to a solid one.
          className={`ov-btn ov-spare split-caret ${override === "spare" ? "active" : ""} ${
            expired ? "expired" : ""
          }`}
          disabled={pending}
          aria-haspopup="menu"
          aria-expanded={menuAt !== null}
          aria-label="Choose how long to keep it"
          onClick={toggleMenu}
        >
          <CaretDownGlyph />
        </button>
      </span>
      {menuAt && (
        <SpareMenu
          at={menuAt}
          defaultDays={defaultDays}
          triggerRef={caretRef}
          onPick={(spareDays) => {
            setMenuAt(null);
            onSet("spare", spareDays);
          }}
          onClose={() => setMenuAt(null)}
        />
      )}
      {!hideReap && (
        <button
          type="button"
          className={`ov-btn ov-reap ${override === "reap" ? "active" : ""}`}
          disabled={pending}
          aria-pressed={override === "reap"}
          onClick={clickReap}
          title={
            override === "reap"
              ? "Marked for reaping. Click to undo"
              : "Force this onto the reap list"
          }
        >
          <ScytheIcon /> {override === "reap" ? "Reaping" : "Reap"}
        </button>
      )}
    </div>
  );
}

/** The length menu behind the Spare chevron: quick day-presets, Forever, and a Custom entry
 *  that expands to a days box. The operator's default is tagged. Picking a length spares the
 *  item at once -- the menu is the action, not a form. Portaled to <body> and rendered
 *  position:fixed at `at`: the card clips its overflow AND its `.card-side` is a z-index:2
 *  stacking context (`.card > *`), which a fixed child alone can't escape -- so a later card's
 *  score badge, spare button, or tooltip paints over an in-card menu. The portal lifts it to the
 *  root stacking context so its own z-index wins. Closed on an outside click, Escape, or a
 *  scroll that would strand it off its anchor. */
function SpareMenu({
  at,
  defaultDays,
  triggerRef,
  onPick,
  onClose,
}: {
  at: MenuPos;
  defaultDays: number;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onPick: (days: number) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [custom, setCustom] = useState(false);
  // Held as free text so the box types and clears naturally; clamped to [1, 3650] only when
  // Spare is pressed, never mid-keystroke (which would snap a half-typed number).
  const [customText, setCustomText] = useState(String(defaultDays > 0 ? defaultDays : 30));
  const spareCustom = () =>
    onPick(Math.max(1, Math.min(3650, Math.floor(Number(customText) || 1))));

  // Read fresh inside the scroll handler without re-subscribing the listeners on each keystroke
  // (rule 19: useRef for a cross-render flag, stable effect deps).
  const customRef = useRef(custom);
  customRef.current = custom;
  // `onClose` the same way, and for the same reason: OverrideControls allocates it fresh on
  // every render, so depending on it tore down and re-added all three listeners on every
  // render while the menu was open -- including on every keystroke in the Custom-length box,
  // the one place this menu re-renders in a tight loop (P-8). The file already avoided this
  // for `custom`; this is the other half.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const onClose = () => closeRef.current();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      // The chevron is its own toggle; leave it out of the outside-close so a click there
      // doesn't close-then-reopen the menu.
      if (!ref.current?.contains(t) && !triggerRef.current?.contains(t)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onScroll = () => {
      // While the Custom-length input is open, ignore scroll: on a phone the virtual keyboard
      // opening scrolls the viewport to reveal the focused field, and a scroll-close would then
      // dismiss the menu before a digit is typed. Outside-click and Escape still close it (U-4).
      if (customRef.current) return;
      onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [triggerRef]);

  // The day-rows: the presets, plus the operator's default when it is a custom number not
  // already among them, sorted so the ladder reads low to high.
  const dayRows = Array.from(
    new Set([...(defaultDays > 0 ? [defaultDays] : []), ...SPARE_PRESETS]),
  ).sort((a, b) => a - b);

  return createPortal(
    <div
      ref={ref}
      className="dur-menu"
      role="menu"
      aria-label="Spare this item for"
      style={{
        position: "fixed",
        left: at.left,
        ...(at.top !== undefined ? { top: at.top } : { bottom: at.bottom }),
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="dur-head">Spare for…</div>
      {dayRows.map((d) => (
        <button key={d} type="button" role="menuitem" className="dur-mi" onClick={() => onPick(d)}>
          <span className="mi-glyph">
            <ClockGlyph />
          </span>
          <span className="mi-label">{d} days</span>
          {d === defaultDays && <span className="mi-tag">Default</span>}
        </button>
      ))}
      <button type="button" role="menuitem" className="dur-mi" onClick={() => onPick(0)}>
        <span className="mi-glyph">
          <span className="infinity" aria-hidden="true">
            ∞
          </span>
        </span>
        <span className="mi-label">Forever</span>
        {defaultDays === 0 && <span className="mi-tag">Default</span>}
      </button>
      <div className="dur-div" />
      {custom ? (
        <div className="dur-custom">
          <span className="qty qty-narrow">
            <input
              type="number"
              min={1}
              max={3650}
              value={customText}
              autoFocus
              aria-label="Custom spare length in days"
              onChange={(e) => setCustomText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") spareCustom();
              }}
            />
            <span className="qty-suffix" aria-hidden="true">
              days
            </span>
          </span>
          <button type="button" className="dur-spare-go" onClick={spareCustom}>
            Spare
          </button>
        </div>
      ) : (
        <button type="button" role="menuitem" className="dur-mi" onClick={() => setCustom(true)}>
          <span className="mi-glyph mi-pen">
            <PenGlyph />
          </span>
          <span className="mi-label">Custom length…</span>
        </button>
      )}
    </div>,
    document.body,
  );
}

/** The note beside a season's Spare/Reap when a *whole-show* decision is what keeps or
 *  reaps it -- so the operator knows the season control toggles only the season's OWN
 *  decision, not the show's. Its wording turns on how the season's own decision relates to
 *  the show's: absent (the show decides), the same (clearing this one changes nothing), or
 *  opposite (the season's own decision wins). Renders nothing when no show decision covers it,
 *  so a movie or an untouched-show season shows no note. The glyph tracks the item's REAL
 *  fate, so the note never contradicts the row's chip. */
export function KeptByShowNote({
  own,
  showOverride,
  effective,
  className = "",
}: {
  own: Override | null;
  showOverride: Override | null;
  /** The row's ``override_effective``: false means a reap the engine can't honor yet (held). */
  effective: boolean | null;
  className?: string;
}) {
  if (!showOverride) return null;
  const fate = own ?? showOverride; // an item's own decision wins over its show's
  // A reap the engine can't honor yet (streaming now, a structural gate) is HELD, not done, so
  // the note must never promise removal for it -- it matches the chip's "kept for now" (U-1).
  const heldReap = fate === "reap" && effective === false;
  let body;
  if (!own) {
    body =
      showOverride === "spare" ? (
        <>
          <b>The whole show is spared</b>, so this season is kept. Undo it on the show.
        </>
      ) : heldReap ? (
        <>
          <b>The whole show is set to reap</b>, but this season is <b>kept for now</b>. Undo it
          on the show.
        </>
      ) : (
        <>
          <b>The whole show is set to reap</b>, so this season will be removed. Undo it on the
          show.
        </>
      );
  } else if (own === showOverride) {
    body =
      showOverride === "spare" ? (
        <>
          The whole show is <b>also spared</b>, so clearing this one won't remove it.
        </>
      ) : (
        <>
          The whole show is <b>also set to reap</b>, so clearing this one won't keep it.
        </>
      );
  } else {
    body =
      own === "reap" ? (
        heldReap ? (
          <>
            You reaped this season, but it is <b>kept for now</b>, even though the whole show is
            spared.
          </>
        ) : (
          <>
            You reaped this season, so it <b>will be removed</b> even though the whole show is
            spared.
          </>
        )
      ) : (
        <>
          You spared this season, so it <b>stays</b> even though the whole show is set to reap.
        </>
      );
  }
  return (
    <p className={`kept-note kept-${fate} ${className}`.trim()}>
      <span className="kept-note-mark" aria-hidden="true">
        {fate === "spare" ? <span className="mk-inf">∞</span> : <ScytheIcon />}
      </span>
      <span>{body}</span>
    </p>
  );
}

/** The resting decision marker: when a hand override is in force, the card rests as a small
 *  icon of that decision (∞ spared, scythe reaped) where the buttons sit, bottom-right. The
 *  hover rules fade it out as the buttons arrive, so the two never show together. Decorative:
 *  the same decision is named by the card's override chip and by the buttons themselves. */
export function OverrideMark({
  override,
  spareExpiresAt = null,
}: {
  override: Override | null;
  /** For a spare, when it stops keeping the item (ISO), or null for forever. A forever spare
   *  rests as ∞; a timed one rests as the clock plus its days left ("27d"); an expired one
   *  rests as the DASHED clock and "0d" -- still keeping the file (only a scan realizes the
   *  clock), but no longer a live decision, the same distinction the button and chip draw. */
  spareExpiresAt?: string | null;
}) {
  if (!override) return null;
  if (override !== "spare") {
    return (
      <span className="override-mark reap" aria-hidden="true">
        <ScytheIcon />
      </span>
    );
  }
  const remaining = spareRemaining(spareExpiresAt);
  return (
    <span className={`override-mark spare ${remaining.expired ? "expired" : ""}`} aria-hidden="true">
      {remaining.forever ? (
        <span className="mk-inf">∞</span>
      ) : (
        <>
          <ClockGlyph dashed={remaining.expired} />
          <span className="mk-count">{remaining.short}</span>
        </>
      )}
    </span>
  );
}
