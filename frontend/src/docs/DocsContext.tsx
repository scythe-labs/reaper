// SPDX-License-Identifier: AGPL-3.0-or-later
//
// One place opens the docs, from anywhere. Any component under the provider calls
// useDocs().openDoc(id, anchor?) and the single DocsModal mounted here shows it. This is the
// "linkable from within the app" contract: a Help button, a warning notice, or an empty
// state can all deep-link into the same docs without each owning a modal.

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useBackGuard } from "../backnav";
import { DocsModal } from "./DocsModal";

type DocsApi = { openDoc: (id: string, anchor?: string) => void };

const DocsCtx = createContext<DocsApi | null>(null);

export function DocsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<{ docId: string; anchor: string | undefined; nonce: number } | null>(
    null,
  );

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
        <DocsModal
          docId={target.docId}
          anchor={target.anchor}
          nonce={target.nonce}
          onClose={() => setTarget(null)}
          onNavigate={(id, anchor) => setTarget((prev) => ({ docId: id, anchor, nonce: (prev?.nonce ?? 0) + 1 }))}
        />
      )}
    </DocsCtx.Provider>
  );
}

export function useDocs(): DocsApi {
  const ctx = useContext(DocsCtx);
  if (!ctx) throw new Error("useDocs must be used within a DocsProvider");
  return ctx;
}
