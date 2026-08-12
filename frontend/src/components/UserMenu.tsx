import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type AuthUser } from "../api";
import { useBackGuard } from "../backnav";
import { useUpdateStatus } from "../updateStatus";
import { Notice } from "./Notice";

/** The signed-in identity, with a panel to sign out.
 *
 *  A disclosure, not an ARIA menu: it is a button that shows and hides a small panel, and
 *  it behaves like one (click or Tab away to dismiss, Escape to close). It used to claim
 *  role="menu", which promises arrow-key navigation between menu items that was never
 *  implemented, on a panel whose first child is a heading rather than an item. The honest
 *  simpler role is the one whose keyboard contract this actually keeps. */
export function UserMenu({ user, onGoToAbout }: { user: AuthUser; onGoToAbout: () => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // The light and the menu item render only while an update actually exists; every
  // could-not-answer state renders neither, so the chip never nags on a guess. The
  // words ride the chip's accessible name -- the light itself is decoration.
  const update = useUpdateStatus();
  const updateAvailable = update.data?.update_available === true;

  // A mutation, not a fire-and-forget async onClick: a sign-out that fails must say so.
  // The session would still be live, and a swallowed error leaves the menu open with the
  // user still signed in and nothing to explain why.
  const signOut = useMutation({
    mutationFn: () => api.logout(),
    // A sign-out that WORKED is written, not asked about -- the same call `main.tsx` makes for
    // every other 401, for the same reason. `noteAuthFailure` (api.ts) exempts the whole
    // `/api/auth/` PREFIX, which is seven routes and this key's own read among them, so refetching
    // it here answers a dead session with a query ERROR while React Query still holds the last
    // good user beside it, and `App.tsx`'s login gate reads that as still signed in. Asking left
    // operator on their own dashboard until an unrelated poll happened to 401.
    onSuccess: () => queryClient.setQueryData(["me"], null),
    // A failure is the opposite question, and the refetch is the right way to ask it: the session
    // may well still be live, and the answer is whatever `/api/auth/me` says.
    onError: () => void queryClient.invalidateQueries({ queryKey: ["me"] }),
  });

  // While the sign-out is running or has failed, the panel stays put. Disabling the focused
  // Sign out button moves focus off it, which some browsers report as focus leaving the
  // whole menu -- closing the panel would then throw away the only place the failure is
  // ever shown, and it would come back stale the next time the menu opened.
  const keepOpen = signOut.isPending || signOut.isError;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (keepOpen) return;
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open, keepOpen]);

  // Clicking away closed it; tabbing away did not, which left the panel hanging open over
  // a page the keyboard had already moved on from.
  const onBlur = (e: React.FocusEvent<HTMLDivElement>) => {
    if (keepOpen) return;
    if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
  };
  // Escape closes and hands focus back to the chip, so the keyboard is where it started.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "Escape" || !open) return;
    setOpen(false);
    triggerRef.current?.focus();
  };

  // Opening starts clean: a failure from a previous attempt is history, not news.
  const toggle = () => {
    if (!open) signOut.reset();
    setOpen((v) => !v);
  };

  // Back closes the menu instead of leaving Reaper. Held open while a sign-out is pending or
  // failed, matching the outside-click guard, so the failure message is never yanked away.
  useBackGuard(open && !keepOpen, () => setOpen(false));

  const initial = user.username.slice(0, 1).toUpperCase();

  return (
    <div className="user-menu" ref={ref} onBlur={onBlur} onKeyDown={onKeyDown}>
      <button
        className="user-chip"
        ref={triggerRef}
        onClick={toggle}
        aria-expanded={open}
        aria-label={updateAvailable ? `${user.username}, update available` : undefined}
      >
        {user.thumb_url ? (
          <img src={user.thumb_url} alt="" className="user-avatar" />
        ) : (
          <span className="user-avatar user-avatar-fallback">{initial}</span>
        )}
        <span className="user-name">{user.username}</span>
        {updateAvailable && <span className="update-light" aria-hidden="true" />}
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="chevron">
          <path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <div className="user-dropdown">
          <div className="user-dropdown-head">
            <div className="user-name">{user.username}</div>
            <div className="muted user-provider">
              {user.provider === "plex" ? "Plex account" : "Local account"}
            </div>
          </div>
          {updateAvailable && (
            <button
              className="user-dropdown-item user-dropdown-update"
              onClick={() => {
                setOpen(false);
                onGoToAbout();
              }}
            >
              Update available
              <span className="update-light" aria-hidden="true" />
            </button>
          )}
          <button
            className="user-dropdown-item"
            onClick={() => signOut.mutate()}
            disabled={signOut.isPending}
          >
            {signOut.isPending ? "Signing out…" : "Sign out"}
          </button>
          {signOut.isError && (
            <Notice tone="error" inline>
              Couldn't sign you out. Try again.
            </Notice>
          )}
        </div>
      )}
    </div>
  );
}
