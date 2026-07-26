// SPDX-License-Identifier: AGPL-3.0-or-later
//
// One place opens the docs, from anywhere. Any component under the provider calls
// useDocs().openDoc(id, anchor?) and the single DocsModal mounted here shows it. This is the
// "linkable from within the app" contract: a Help button, a warning notice, or an empty
// state can all deep-link into the same docs without each owning a modal.

import {
  createContext,
  lazy,
  Suspense,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useBackGuard } from "../backnav";

// Every word of every document, only once someone asks to read one. The docs are prose, not
// app code, and they used to ride in the first chunk the queue waits on (P-4). Nothing is
// rendered until `target` is set, so the fetch starts on the first Help press.
const DocsModal = lazy(async () => ({ default: (await import("./DocsModal")).DocsModal }));

type DocsApi = { openDoc: (id: string, anchor?: string) => void };

const DocsCtx = createContext<DocsApi | null>(null);

export function DocsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<{
    docId: string;
    anchor: string | undefined;
    nonce: number;
  } | null>(null);

  const openDoc = useCallback((id: string, anchor?: string) => {
    // A fresh nonce every call, so re-opening the same doc and anchor re-scrolls.
    setTarget((prev) => ({ docId: id, anchor, nonce: (prev?.nonce ?? 0) + 1 }));
  }, []);

  const api = useMemo(() => ({ openDoc }), [openDoc]);

  // Back closes the docs overlay instead of leaving Reaper.
  useBackGuard(target !== null, () => setTarget(null));

  return (
    <DocsCtx.Provider value={api}>
      {children}
      {target && (
        // No fallback: the docs are a modal over the page, and a spinner in the scrim for the
        // moment it takes to fetch would flash more than it informs.
        <Suspense fallback={null}>
          <DocsModal
            docId={target.docId}
            anchor={target.anchor}
            nonce={target.nonce}
            onClose={() => setTarget(null)}
            onNavigate={(id, anchor) =>
              setTarget((prev) => ({ docId: id, anchor, nonce: (prev?.nonce ?? 0) + 1 }))
            }
          />
        </Suspense>
      )}
    </DocsCtx.Provider>
  );
}

export function useDocs(): DocsApi {
  const ctx = useContext(DocsCtx);
  if (!ctx) throw new Error("useDocs must be used within a DocsProvider");
  return ctx;
}
