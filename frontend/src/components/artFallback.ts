import { useEffect, useRef, useState } from "react";

/** The art-then-poster ladder: ask Plex for the wide art, fall back once to the poster when a
 *  title has no separate backdrop, then give up. A null `src` tells the caller to render
 *  nothing at all.
 *
 *  One hook serves two components whose markup shares nothing, `ReviewQueue`'s `Backdrop` and
 *  `WhyPanel`'s `WhyHero`.
 *
 *  A null `posterUrl` asks for nothing and gives up at once, which is `Backdrop`'s contract.
 *  `WhyHero` only ever mounts behind a `poster_url &&` guard, so it never reaches that branch;
 *  `artFallback.test.tsx` drives the null case through the hook directly, since no component
 *  can. `setSrc(posterUrl)` with no url already produces the give-up value, so there is no
 *  separate check for `posterUrl` being truthy before falling back.
 *
 *  The effect resets the fallback flag whenever `posterUrl` changes. When the selected item
 *  changes without an unmount in between (the next item's detail was already cached, so the
 *  panel goes straight from one item to the next) the component is reused, and without the
 *  effect the fallback flag would stay set from the previous item, showing its art under the
 *  new title.
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
