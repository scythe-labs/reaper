import { useTranslation } from "react-i18next";
import { PanelFallback } from "./WhyShell";

/** What the why-panel's column shows while the reasoning is loading, or when it could not be
 *  loaded at all. `PanelFallback` in `WhyShell.tsx` is the whole of it, `ScalesPanelFallback`
 *  its twin, and these three strings all that differ.
 *
 *  Never add "The item itself is unaffected." to this failure message: this panel opens
 *  beside queue cards carrying Spare and Reap, so an operator who has just pressed one
 *  would read that line as their decision not having landed. */
export function WhyPanelFallback({ error, onClose }: { error: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <PanelFallback
      error={error}
      onClose={onClose}
      waiting={t("why.panel.fallback.waiting")}
      loading={t("why.panel.fallback.loading")}
      failure={t("why.panel.fallback.failure")}
    />
  );
}
