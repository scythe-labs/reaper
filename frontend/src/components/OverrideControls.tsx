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
  useId,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { Trans, useTranslation } from "react-i18next";
import type { Override } from "../api";
import { useDialogFocus } from "../focus";
import { spareRemaining } from "../format";
import { trapTab } from "./ModalShell";
import { useFixedMenu, type MenuPos } from "./popoverFit";
import { FixedQuantity } from "./QuantityInput";
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
  const { t } = useTranslation();
  const defaultDays = useDefaultSpareDays();
  const caretRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  // While the Custom-length box is open, a scroll must not close the menu: on a phone the
  // virtual keyboard opening scrolls the viewport to reveal the focused field, and a
  // scroll-close would dismiss the menu before a digit is typed. Set by `SpareMenu` itself
  // (the only place that knows), read by the hook's own scroll listener.
  const ignoreScrollRef = useRef(false);
  const {
    pos: menuAt,
    menuRef,
    toggle: toggleMenu,
    close: closeMenu,
  } = useFixedMenu<HTMLDivElement>(caretRef, { width: 224, height: 250, ignoreScrollRef });
  // Picking a length and clearing a spare each close the menu AND start a mutation, and the two
  // land in ONE batched commit -- so `pending` has already disabled the caret (below) by the time
  // `useDialogFocus`'s restore runs in the later passive phase, and `.focus()` on a disabled
  // button is a silent no-op. Focus fell to <body> on the menu's two most common exits, the next
  // Tab restarting above the whole queue: the exact failure this control was rewritten to remove,
  // on the press that KEEPS a file. Re-issue the restore once the mutation settles and the caret
  // can hold focus again. Escape and the outside click need none of this -- they start no
  // mutation, so the caret is still enabled when the hook's own restore fires.
  const returnToCaret = useRef(false);
  useEffect(() => {
    if (pending || !returnToCaret.current) return;
    returnToCaret.current = false;
    caretRef.current?.focus();
  }, [pending]);
  // The same failure on the buttons themselves, and the reason the `aria-pressed` flip below --
  // the app's only announcement that a spare succeeded -- reached nobody. Pressing Spare starts
  // a write, `pending` disables the button that was pressed, and disabling the focused element
  // drops focus to `<body>` in every major browser. By the time the state settled and the flip
  // happened there was no focused element to announce it, on the press that KEEPS a file (#173).
  //
  // Scoping `pending` to the row's own write (`pendingFor` in ReviewQueue) does not fix this on
  // its own -- it is the acting row whose button disables -- so the press is remembered and
  // focus re-issued the moment the wait ends, the shape `returnToCaret` above already uses.
  const pressed_ = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (pending || pressed_.current === null) return;
    const target = pressed_.current;
    pressed_.current = null;
    // Only if the operator has not moved on. A restore is not a yank: focus fell to `<body>`
    // because we disabled the button, so `<body>` (or nothing) is exactly the state to repair.
    if (document.activeElement === document.body || document.activeElement === null) {
      target.focus();
    }
  }, [pending]);

  // Undecided, the button says what a press WILL do (the default length). Spared, it says what
  // is in force on this item -- its own glyph, its own count -- because the default stops
  // mattering the moment there is a real answer. Sparing for 90 days under a Forever default
  // used to leave the button reading "∞ Spared", wrong on both counts.
  //
  // Three spared states, never two. An EXPIRED spare is its own, and it is the one state where
  // this control STOPS BEING A TOGGLE. The other two answer "is this spared" and press to undo
  // themselves. A spent one has nothing left to undo: what the operator came here to do is keep
  // the item again, so it reads "Spare" and a press sets a fresh one. It is not a plain
  // undecided button either -- the dashed dial says a spare ran out right here -- and clearing
  // the spent row moves into the length menu, which has room to say what it does.
  //
  // A count is how much of something is LEFT, so "0d" is not a smaller amount of it; sitting in
  // a pressed green button it read "your active decision, none of it", which is a contradiction
  // rather than a state. ("Expired" cannot go there at all: measured in a real browser it
  // renders "Expir…", since the track -- `--ov-btn-w`, rule 51 -- leaves the label about 47px
  // once the caret, the padding and the glyph are out.) The word lives in the tooltip, and the
  // roomy footer below spells the whole state out where there is space.
  const spared = override === "spare";
  const remaining = spareRemaining(spareExpiresAt);
  const counting = spared && !remaining.forever && !remaining.expired;
  const expired = spared && remaining.expired;
  // `pressed` is the toggle question, and an expired spare answers no even though `spared` is
  // yes: it drives the fill, `aria-pressed` and what a press does, so all three stay in step.
  const pressed = spared && !expired;
  const spareLabel = !spared
    ? t("reviewQueue.overrideControls.spare")
    : expired
      ? roomy
        ? t("reviewQueue.overrideControls.spareAgain")
        : t("reviewQueue.overrideControls.spare")
      : counting
        ? roomy
          ? t("reviewQueue.overrideControls.sparedCount", { short: remaining.short })
          : remaining.short
        : t("reviewQueue.overrideControls.spared");

  const clickSpare = (e: ReactMouseEvent) => {
    e.stopPropagation();
    pressed_.current = e.currentTarget as HTMLButtonElement;
    // Expired presses through to a NEW spare rather than clearing the spent one (see above).
    pressed ? onClear() : onSet("spare", defaultDays);
  };
  const clickReap = (e: ReactMouseEvent) => {
    e.stopPropagation();
    pressed_.current = e.currentTarget as HTMLButtonElement;
    override === "reap" ? onClear() : onSet("reap");
  };

  return (
    <div
      className={`override-controls ${menuAt ? "menu-open" : ""}`}
      role="group"
      aria-label={t("reviewQueue.overrideControls.groupAria")}
      // No Enter/Space guard here any more. It existed because every row and card holding these
      // buttons was a `role="button"` with its own key handler calling preventDefault, which
      // canceled the button's activation and opened the panel instead (B-7). Those containers
      // are plain again (#169), and the buttons were never validly nested inside them to begin
      // with. Nothing above this cancels an activation, so a guard here would be a comment
      // claiming a safeguard against a handler that does not exist (rule 7/24).
    >
      <span className="ov-split">
        <button
          type="button"
          className={`ov-btn ov-spare split-main ${pressed ? "active" : ""} ${
            expired ? "expired" : ""
          }`}
          disabled={pending}
          // Not pressed once spent, and that is not a lie about the spare still being on file:
          // this control is a toggle for "is this spared by hand", and a spent spare is one a
          // press no longer turns off. Its own state is still announced, by the name below.
          aria-pressed={pressed}
          // On the narrow surfaces the visible label is abbreviated to fit the fixed track, so
          // name the button in full for a screen reader. The visible text always stays a
          // substring of that name, which is what WCAG 2.5.3 (Label in Name) asks for -- so the
          // spent name leads with the word "Spare" the button shows, and adds what a press does
          // rather than reading like a status.
          aria-label={
            expired
              ? t("reviewQueue.overrideControls.spareAgainExpiredAria")
              : roomy || !counting
                ? undefined
                : t("reviewQueue.overrideControls.sparedLeftAria", { short: remaining.short })
          }
          onClick={clickSpare}
          title={
            expired
              ? // `expiredOn`, never `note`: this control knows only THIS item's own spare, and
                // note's "still kept until the next scan judges it again" is false wherever a
                // show-level spare outlasts it. What is still keeping the file is the covering
                // spare's question, answered by the row's chip and by KeptByShowNote beside it.
                t("reviewQueue.overrideControls.spareAgainTitle", {
                  expiredOn: remaining.expiredOn,
                })
              : counting
                ? t("reviewQueue.overrideControls.spareJudgeAgainTitle", { until: remaining.until })
                : spared
                  ? t("reviewQueue.overrideControls.sparedJudgeAgainTitle")
                  : defaultDays > 0
                    ? t("reviewQueue.overrideControls.spareForDaysTitle", { n: defaultDays })
                    : t("reviewQueue.overrideControls.spareForeverTitle")
          }
        >
          {/* `days` only picks the glyph's shape here: ∞ for a forever spare, the clock for a
              timed one. A spent spare keeps the clock (dashed, via `.expired`), so the button
              still says a spare ran out here rather than reading as never-decided. */}
          <SpareGlyph days={pressed ? (remaining.forever ? 0 : 1) : expired ? 1 : defaultDays} />{" "}
          <span className="ov-label">{spareLabel}</span>
        </button>
        <button
          ref={caretRef}
          type="button"
          // The caret follows the main button exactly -- same `pressed`, same `.expired` -- so
          // the pair reads as one control rather than a dashed half joined to a solid one.
          className={`ov-btn ov-spare split-caret ${pressed ? "active" : ""} ${
            expired ? "expired" : ""
          }`}
          disabled={pending}
          // A disclosure, not an ARIA menu, and for the reason App's UserMenu records: this
          // never implemented the arrow-key navigation `role="menu"` promises. `aria-controls`
          // is what ties the caret to a panel the portal puts at the far end of <body>.
          aria-expanded={menuAt !== null}
          aria-controls={menuAt !== null ? menuId : undefined}
          aria-label={t("reviewQueue.overrideControls.chooseLengthAria")}
          onClick={toggleMenu}
        >
          <CaretDownGlyph />
        </button>
      </span>
      {menuAt && (
        <SpareMenu
          at={menuAt}
          menuId={menuId}
          menuRef={menuRef}
          defaultDays={defaultDays}
          ignoreScrollRef={ignoreScrollRef}
          onPick={(spareDays) => {
            returnToCaret.current = true;
            closeMenu();
            onSet("spare", spareDays);
          }}
          onClear={
            spared
              ? () => {
                  returnToCaret.current = true;
                  closeMenu();
                  onClear();
                }
              : undefined
          }
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
              ? t("reviewQueue.overrideControls.reapMarkedTitle")
              : t("reviewQueue.overrideControls.reapForceTitle")
          }
        >
          <ScytheIcon />{" "}
          {override === "reap"
            ? t("reviewQueue.overrideControls.reaping")
            : t("reviewQueue.overrideControls.reap")}
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
 *  root stacking context so its own z-index wins.
 *
 *  Its open/close state, its clamped position, and the outside-click/Escape/scroll dismissal all
 *  live in `useFixedMenu` (`components/popoverFit.ts`), called by `OverrideControls` above --
 *  `menuRef` and `ignoreScrollRef` are that hook's, threaded down so this component only has to
 *  draw. */
function SpareMenu({
  at,
  menuId,
  menuRef,
  defaultDays,
  ignoreScrollRef,
  onPick,
  onClear,
}: {
  at: MenuPos;
  /** Pointed at by the caret's `aria-controls`. The portal puts this panel at the end of
   *  `<body>`, nowhere near the button that opened it, so the association has to be stated. */
  menuId: string;
  menuRef: RefObject<HTMLDivElement | null>;
  defaultDays: number;
  /** Set to whether the Custom-length box is open, each render -- the hook's scroll listener
   *  reads it so a phone's virtual keyboard opening cannot dismiss the menu before a digit is
   *  typed (U-4). Owned by the hook; this component only writes it. */
  ignoreScrollRef: RefObject<boolean>;
  onPick: (days: number) => void;
  /** Clears the spare on this control's own level. Omitted when there is nothing to clear,
   *  which is what keeps the row off an undecided item's menu. */
  onClear?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
  // The portal is why focus-handling is not optional. Rendered at the end of <body>, the menu's
  // items sit nowhere near the caret in the Tab order, so opening it and pressing Tab used to
  // walk on to the next control INSIDE the card while the menu hung open beside it, its own rows
  // reachable only by tabbing through the remainder of the page. Focus goes in, and Tab stays in.
  //
  // It hands focus back from here only on the exits that start no mutation: Escape, and Back. A
  // pick and a Clear disable the caret in the same commit that closes the menu, so their restore
  // cannot run from here at all -- `returnToCaret` in OverrideControls re-issues it once the
  // mutation settles. An outside click deliberately restores nothing: the click is the operator
  // saying where they want to be, which is the same reading the filter popovers take.
  // Focus lands on the FIRST DAY ROW, not on the container. On iOS a `role="group"` with children
  // is never a `UIAccessibilityElement` (WebKit's `determineIsAccessibilityElement`), so focusing
  // the container put the VoiceOver cursor nowhere at all and a swipe-right from the caret walked
  // the panel BEHIND this menu -- the menu's own rows reachable only by swiping to the end of the
  // page, since the portal puts them last in reading order (#206). Landing on a real button is the
  // APG Menu Button pattern and costs nothing on the platforms that were already fine.
  //
  // The two rejected repairs, both verified worse: portaling into `.why` would move the stacking
  // and scroll behavior this menu depends on, and `role="menu"` promises arrow-key navigation this
  // never implemented AND is skipped outright by TalkBack (`browser_accessibility_android.cc`
  // returns false for `kMenu` in `IsInterestingOnAndroid`), which works today.
  const firstRow = useRef<HTMLButtonElement>(null);
  useDialogFocus(menuRef, true, firstRow);
  const [custom, setCustom] = useState(false);
  ignoreScrollRef.current = custom;
  // Held as free text so the box types and clears naturally; clamped to [1, 3650] only when
  // Spare is pressed, never mid-keystroke (which would snap a half-typed number).
  const [customText, setCustomText] = useState(String(defaultDays > 0 ? defaultDays : 30));
  const spareCustom = () =>
    onPick(Math.max(1, Math.min(3650, Math.floor(Number(customText) || 1))));

  // The day-rows: the presets, plus the operator's default when it is a custom number not
  // already among them, sorted so the ladder reads low to high.
  const dayRows = Array.from(
    new Set([...(defaultDays > 0 ? [defaultDays] : []), ...SPARE_PRESETS]),
  ).sort((a, b) => a - b);

  return createPortal(
    <div
      ref={menuRef}
      id={menuId}
      className="dur-menu"
      // Not `role="menu"`: that promises arrow-key navigation between items, which this never
      // implemented, on a panel whose first child is a heading rather than an item. App's
      // UserMenu records the same defect being fixed the same way. The honest role is the one
      // whose keyboard contract this actually keeps -- a labeled group of buttons.
      role="group"
      aria-label={t("reviewQueue.overrideControls.menuAria")}
      tabIndex={-1}
      onKeyDown={(e) => trapTab(e, menuRef.current)}
      style={{
        position: "fixed",
        insetInlineStart: at.start,
        ...(at.top !== undefined ? { top: at.top } : { bottom: at.bottom }),
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="dur-head">{t("reviewQueue.overrideControls.menuHeading")}</div>
      {dayRows.map((d, i) => (
        <button
          key={d}
          type="button"
          className="dur-mi"
          ref={i === 0 ? firstRow : undefined}
          onClick={() => onPick(d)}
        >
          <span className="mi-glyph">
            <ClockGlyph />
          </span>
          <span className="mi-label">
            {t("reviewQueue.overrideControls.dayRowLabel", { n: d })}
          </span>
          {d === defaultDays && (
            <span className="mi-tag">{t("reviewQueue.overrideControls.defaultTag")}</span>
          )}
        </button>
      ))}
      <button type="button" className="dur-mi" onClick={() => onPick(0)}>
        <span className="mi-glyph">
          <span className="infinity" aria-hidden="true">
            ∞
          </span>
        </span>
        <span className="mi-label">{t("reviewQueue.overrideControls.forever")}</span>
        {defaultDays === 0 && (
          <span className="mi-tag">{t("reviewQueue.overrideControls.defaultTag")}</span>
        )}
      </button>
      <div className="dur-div" />
      {custom ? (
        <div className="dur-custom">
          <FixedQuantity
            value={customText}
            onChange={(n) => setCustomText(String(n))}
            suffix={t("reviewQueue.overrideControls.customDaysUnit")}
            min={1}
            max={3650}
            step={1}
            width="narrow"
            // "Custom spare length", not "...in days": FixedQuantity binds the visible "days"
            // suffix as the box's description, so the unit is spoken after the value. Naming it
            // here too would read "custom spare length in days, 30, days" -- the stutter the
            // shared control's own comment warns about, and the reason the unit is a
            // description rather than part of the name.
            ariaLabel={t("reviewQueue.overrideControls.customLengthAria")}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter") spareCustom();
            }}
          />
          <button type="button" className="dur-spare-go" onClick={spareCustom}>
            {t("reviewQueue.overrideControls.spare")}
          </button>
        </div>
      ) : (
        <button type="button" className="dur-mi" onClick={() => setCustom(true)}>
          <span className="mi-glyph mi-pen">
            <PenGlyph />
          </span>
          <span className="mi-label">{t("reviewQueue.overrideControls.customLength")}</span>
        </button>
      )}
      {/* Where clearing lives once the main button stops being the toggle. A LIVE spare still
          clears from that button (press it again), so this row is the spent spare's only way
          to drop the row without keeping it longer -- and unlike a bare toggle it has room to
          say what it does. The next scan deletes a spent row anyway
          (`whitelist.purge_expired_spares`), so this is that, now. */}
      {onClear && (
        <button type="button" className="dur-mi dur-clear" onClick={onClear}>
          <span className="mi-label">{t("reviewQueue.overrideControls.clearThisSpare")}</span>
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
 *  opposite (the season's own decision wins, and clearing it hands the season to the show's).
 *  Every branch that can be cleared names what clearing does, in both directions -- the
 *  harmless one and the one that puts a file back on the reap list.
 *  Renders nothing when no show decision covers it,
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
        <Trans i18nKey="reviewQueue.overrideControls.showSparedKept" components={{ b: <b /> }} />
      ) : heldReap ? (
        <Trans i18nKey="reviewQueue.overrideControls.showReapHeldKept" components={{ b: <b /> }} />
      ) : (
        <Trans
          i18nKey="reviewQueue.overrideControls.showReapWillRemove"
          components={{ b: <b /> }}
        />
      );
  } else if (own === showOverride) {
    body =
      showOverride === "spare" ? (
        <Trans i18nKey="reviewQueue.overrideControls.showAlsoSpared" components={{ b: <b /> }} />
      ) : (
        <Trans i18nKey="reviewQueue.overrideControls.showAlsoReap" components={{ b: <b /> }} />
      );
  } else {
    body =
      own === "reap" ? (
        heldReap ? (
          <Trans i18nKey="reviewQueue.overrideControls.ownReapedHeld" components={{ b: <b /> }} />
        ) : (
          <Trans
            i18nKey="reviewQueue.overrideControls.ownReapedRemoved"
            components={{ b: <b /> }}
          />
        )
      ) : (
        // The one direction where clearing this control puts a file on the block, so it is the
        // one that must say so. The harmless twin above already spells its consequence out
        // ("clearing this one won't remove it"); saying nothing here left the note warning the
        // operator only when the click was safe, which runs the wrong way.
        <Trans i18nKey="reviewQueue.overrideControls.ownSparedStays" components={{ b: <b /> }} />
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
   *  rests as ∞; a timed one rests as the clock plus its days left ("27d"); a SPENT one rests
   *  as nothing at all.
   *
   *  This mark is what a row carrying a decision looks like at rest (rule 46), and a spare
   *  whose clock has passed is no longer a decision in force at this level: the button it
   *  hands over to on hover offers a fresh spare rather than an undo, so resting as "0d" over
   *  it announced a decision the control no longer holds. What is still keeping the file is a
   *  different question, and the row's chip and score answer it from the covering spare. */
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
  // Nothing at rest for a spent spare (see the prop's doc): the row shows the plain Spare
  // affordance on hover, so an icon of a decision the button no longer holds would be the two
  // disagreeing about the same key.
  if (remaining.expired) return null;
  return (
    <span className="override-mark spare" aria-hidden="true">
      {remaining.forever ? (
        <span className="mk-inf">∞</span>
      ) : (
        <>
          <ClockGlyph />
          <span className="mk-count">{remaining.short}</span>
        </>
      )}
    </span>
  );
}
