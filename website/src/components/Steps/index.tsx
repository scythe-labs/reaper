// SPDX-License-Identifier: AGPL-3.0-or-later
//
// An ordered procedure. `DocBody` numbers these against a spine; the site does the same, using a
// real <ol> so the count is in the markup rather than painted on, and a screen reader announces
// "list of 4 items" instead of reading decorative digits.

import type { ReactNode } from "react";

export function Steps({ children }: { children: ReactNode }) {
  return <ol className="rp-steps">{children}</ol>;
}

export function Step({ title, children }: { title: string; children: ReactNode }) {
  return (
    <li className="rp-step">
      <div className="rp-step__title">{title}</div>
      <div className="rp-step__body">{children}</div>
    </li>
  );
}
