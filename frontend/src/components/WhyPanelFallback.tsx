import { PanelFallback } from "./WhyShell";

/** What the why-panel's column shows while the reasoning is loading, or when it could not be
 *  loaded at all. `PanelFallback` in `WhyShell.tsx` is the whole of it, `ScalesPanelFallback` its
 *  twin, and these three strings all that differ.
 *
 *  The failure used to add "The item itself is unaffected." It came out rather than being
 *  mirrored: this panel opens beside queue cards carrying Spare and Reap, so an operator who has
 *  just pressed one reads it as their decision not having landed. */
export function WhyPanelFallback({ error, onClose }: { error: boolean; onClose: () => void }) {
  return (
    <PanelFallback
      error={error}
      onClose={onClose}
      waiting="Still loading what Reaper saw about this item."
      loading="Fetching what Reaper saw…"
      failure="Couldn't load the reasons for this item. Close this panel and click the item to try again."
    />
  );
}
