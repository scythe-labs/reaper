import { useEffect, useRef, useState } from "react";

/** The art-then-poster ladder: ask Plex for the wide art, fall back once to the poster when a
 *  title has no separate backdrop, then give up. A null `src` is the caller's cue to render
 *  nothing at all.
 *
 *  One declaration for two sites whose markup shares nothing, `ReviewQueue`'s `Backdrop` and
 *  `WhyPanel`'s `WhyHero`. Each copy used to carry a comment pointing at the other (rule 72).
 *
 *  A null `posterUrl` asks for nothing and gives up at once, which is `Backdrop`'s contract.
 *  `WhyHero` is only ever mounted behind a `poster_url &&` guard, so it reaches no branch this
 *  changes; `artFallback.test.tsx` drives the null lane through the hook, since no component
 *  can. `Backdrop`'s copy also guarded the first rung on `posterUrl` being truthy, and that arm
 *  is dropped rather than carried: `setSrc(posterUrl)` with no url IS the give-up value, so the
 *  guard's two sides were indistinguishable in every state and neither test nor reader could
 *  tell them apart (rule 118).
 *
 *  The reset travels with it. When the selected item changes without an unmount in between (the
 *  next item's detail was already cached, so the panel goes A to B directly) the component is
 *  reused, and without the effect the fallback flag latches: the previous item's art stays under
 *  the new title, which is the mismatch the why-panel exists to avoid. Rule 19.
 */
export function useArtFallback(posterUrl: string | null): {
  src: string | null;
  onError: () => void;
} {
  const [src, setSrc] = useState(posterUrl ? `${posterUrl}?kind=art` : null);
  const fellBack = useRef(false);

  useEffect(() => {
    fellBack.current = false;
    setSrc(posterUrl ? `${posterUrl}?kind=art` : null);
  }, [posterUrl]);

  const onError = () => {
    if (fellBack.current) {
      setSrc(null);
      return;
    }
    fellBack.current = true;
    setSrc(posterUrl);
  };

  return { src, onError };
}
