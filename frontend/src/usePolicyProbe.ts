// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Asking the engine what a policy rule would do at a value, at the pace of a drag.
//
// Shared by every probe kind, present and future: the request is the discriminated
// `PolicyProbe` and the answer is one shape, so a second kind reuses this file untouched.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type PolicyProbe, type PolicyProbeResult } from "./api";

/** What the surface asking has to render. All three states, because a probe that shows a
 *  stale number while a new one is in flight, or nothing at all when the read failed, is a
 *  confident answer to a question nobody answered. */
export interface Probe {
  answer: PolicyProbeResult | null;
  pending: boolean;
  failed: boolean;
}

/** How long the policy editor's reads wait for a drag to stop, so one lands when the
 *  operator lets go rather than one per pixel. Shared with `PolicyEditor`'s simulate/validate
 *  timer, which imports this constant rather than spelling the number again.
 *
 *  The review queue's search box debounces on its own 250 and says at its own site why it is
 *  not this one. */
export const SETTLE_MS = 250;

export function usePolicyProbe(probe: PolicyProbe | null): Probe {
  const [settled, setSettled] = useState<PolicyProbe | null>(null);
  // A fresh object every render cannot be an effect dependency, so the timer keys on the
  // request's content instead. This is a cache key, not a dirty check: nothing here decides
  // whether the request has changed by serializing it and comparing, so an equal key that
  // re-fired would cost one identical request at worst.
  const key = probe === null ? null : JSON.stringify(probe);

  useEffect(() => {
    const id = setTimeout(() => setSettled(probe), SETTLE_MS);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` IS `probe`, serialized.
  }, [key]);

  const { data, error, isFetching } = useQuery({
    queryKey: ["policy-probe", settled],
    queryFn: () => api.probePolicy(settled!),
    enabled: settled !== null,
    // No `placeholderData`: holding the last answer would contradict the top of this file.
    // `pending` covers the debounce and the request, and the caller drops `answer` for the
    // whole of it, so a held number could only ever reach the screen as an answer about a
    // value the operator has already moved past.
    retry: false,
  });

  return {
    answer: error ? null : (data ?? null),
    // Waiting covers the debounce window too: between the last drag and the request there
    // is no answer for the value on screen, and the previous one is about a value the
    // operator has already moved past.
    pending: !error && (settled === null || key !== JSON.stringify(settled) || isFetching),
    failed: Boolean(error),
  };
}
