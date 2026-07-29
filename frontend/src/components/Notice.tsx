// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one notice. Every `.notice` in the app is this component, so the question "does a screen
// reader hear this?" is answered once instead of at each site.
//
// It exists because the answer used to be no, everywhere. There were 109 hand-rolled
// `<p className="notice notice-error">` blocks and seven live regions in the whole frontend, and
// not one of the seven was a notice: they were four loading spinners, two scan toasts and the log
// tail. Nothing the app said after an operator pressed something -- not a failed save, not a
// refused switch, not a wrong password on the switch that arms deletion -- was announced at all.
// Rule 18 already called for a shared notice; its absence is exactly why 109 copies drifted.
//
// Why `role="alert"` and not `role="status"`. Nearly every notice here is mounted at the moment
// it has something to say (`{save.error && <Notice …>}`). A polite region inserted into the DOM
// in the same commit as its text is unreliably announced -- several readers only watch regions
// that were already there -- so `role="status"` on a conditionally-rendered node reads as
// correct and stays silent. `role="alert"` is announced on insertion, which is the shape these
// call sites actually have. The two existing bare `role="status"` nodes in `ReviewQueue` have
// this bug today and are not this component's to fix.
//
// `standing` is the opt-out, for a notice that is part of the page whenever some condition holds
// rather than a reaction to anything -- most importantly one that re-renders as the operator
// types, where an alert per keystroke is worse than silence. WCAG 4.1.3 covers messages that
// appear in response to a change; page furniture is read in document order like any other text.
// It is a deliberate declaration, not a default, so a new notice cannot go quiet by omission.

import type { ElementType, ReactNode, Ref } from "react";

export type NoticeTone = "error" | "warn";

export function Notice({
  tone,
  standing = false,
  inline = false,
  className,
  as: Tag = "p",
  children,
  ...rest
}: {
  /** `error` for something that failed or could not be read; `warn` for a caution. Picks the
   *  color AND the lead a reader hears ("Problem: " / "Warning: ", below), so it is a claim
   *  about severity rather than a style choice: pick `warn` for a caution, never for amber. */
  tone: NoticeTone;
  /** True when this text is part of the page rather than a reaction to something, so it must
   *  NOT interrupt. Say why in a comment at the call site. */
  standing?: boolean;
  /** The tighter in-flow variant (`.notice-inline`). */
  inline?: boolean;
  /** Extra layout classes for one call site. */
  className?: string;
  /** `p` unless the layout needs otherwise -- `span` inside a flow of text, `div` where the
   *  notice carries block content of its own. */
  as?: "p" | "div" | "span";
  children: ReactNode;
  /** For a notice focus is moved into: pair with `tabIndex={-1}`. React 19 passes `ref` to a
   *  function component as an ordinary prop, so no forwardRef. */
  ref?: Ref<HTMLElement>;
  /** -1 to make the notice itself focusable without putting it in the tab order. */
  tabIndex?: number;
  id?: string;
}) {
  const classes = [
    "notice",
    tone === "error" ? "notice-error" : "notice-warn",
    inline ? "notice-inline" : null,
    className ?? null,
  ]
    .filter(Boolean)
    .join(" ");
  // `Tag` is a union of three intrinsic elements, whose `ref` types differ (paragraph / div /
  // span). TypeScript resolves the spread against the intersection and rejects the one
  // `Ref<HTMLElement>` the props declare, so the tag is widened here rather than making every
  // caller pick a matching ref type for a layout detail.
  const El = Tag as ElementType;
  return (
    <El className={classes} role={standing ? undefined : "alert"} {...rest}>
      {/* Severity was a color and nothing else: `notice-error` against `notice-warn` was the
          only difference between "this blocks you" and "this is advice", so the two were
          indistinguishable by ear even once the text was reached -- a 1.4.1 failure as much as
          a 4.1.3 one. Visually hidden, because the color already says it to anyone who can see
          it and a visible lead would say twice what the tone shows.

          Here rather than at the call sites for the same reason the role is here: one answer to
          "can a reader tell these apart?", provable once instead of 108 times (rule 18). */}
      <span className="sr-only">{tone === "error" ? "Problem: " : "Warning: "}</span>
      {children}
    </El>
  );
}
