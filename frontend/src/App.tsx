// SPDX-License-Identifier: AGPL-3.0-or-later

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type AuthUser, type Snapshot, type Verdict } from "./api";
import { Fairness } from "./components/Fairness";
import { Login } from "./components/Login";
import { PolicyEditor } from "./components/PolicyEditor";
import { ReapPlan } from "./components/ReapPlan";
import { ReviewQueue } from "./components/ReviewQueue";
import { Settings } from "./components/Settings";
import { SetupWizard } from "./components/SetupWizard";
import { WhyPanel } from "./components/WhyPanel";
import { count, date } from "./format";

type View = "review" | "policy" | "reap" | "fairness" | "settings";

const NAV: { id: View; label: string }[] = [
  { id: "review", label: "Review" },
  { id: "policy", label: "Policy" },
  { id: "reap", label: "Reap" },
  { id: "fairness", label: "Fairness" },
  { id: "settings", label: "Settings" },
];

/** The safety state, stated permanently and without euphemism.
 *
 *  While destructive actions are disabled, Reaper *structurally cannot* delete: the
 *  GuardedTransport refuses every mutating HTTP request, so this is a fact about the
 *  process rather than a promise about the UI. The banner says which regime you are in,
 *  always, because "can this thing delete my library right now?" should never require
 *  reading a settings page to answer. */
function SafetyBanner() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["health"], queryFn: api.health });

  // On the very first fetch we genuinely know nothing yet -- stay quiet rather than flash a
  // state we might immediately contradict.
  if (isLoading) return null;

  // The banner's whole promise is that the regime is stated *always*. If /health can't be
  // reached and React Query has no last-known value to show, we must not just disappear --
  // an absent banner reads as "nothing to worry about". Say the state is unknown, in the
  // amber "we could not look" tone, so it never reads as safe.
  if (isError || !data) {
    return (
      <div className="banner banner-unknown">
        <span className="banner-dot" aria-hidden="true" />
        <span>
          <strong>Safety state unknown.</strong> Reaper couldn't reach the server to confirm
          whether deletion is on. Until it can, treat this as armed and check Settings → Safety.
        </span>
      </div>
    );
  }

  if (!data.destructive_actions_enabled) {
    return (
      <div className="banner banner-safe">
        <span className="banner-dot" aria-hidden="true" />
        <span>
          <strong>Read-only.</strong> Reaper can look but can't remove anything. Turn deletion on
          in Settings → Safety when you're ready.
        </span>
      </div>
    );
  }

  return (
    <div className="banner banner-armed">
      <span className="banner-dot" aria-hidden="true" />
      <span>
        <strong>Deletion is on.</strong> Reaper can remove media you approve, through Sonarr and
        Radarr.
      </span>
    </div>
  );
}

/** A slim freshness line on the Review screen: when the queue was last built, and a loud
 *  note if that scan came back incomplete (the scan control itself now lives in Settings →
 *  Jobs). Without this, the queue gives no sense of how stale it might be. */
function ScanFreshness({ snapshot }: { snapshot: Snapshot | undefined }) {
  if (!snapshot) {
    return (
      <p className="scan-freshness muted">
        No scan has run yet. Run one from Settings → Jobs to fill the queue.
      </p>
    );
  }
  return (
    <p className="scan-freshness muted">
      Last scanned {date(snapshot.created_at)} · {count(snapshot.item_count)} items
      {snapshot.degraded && (
        <span className="freshness-warn">
          {" "}
          · that scan came back incomplete, so Reaper won't act on it
        </span>
      )}
    </p>
  );
}

/** The signed-in identity, with a menu to sign out. */
function UserMenu({ user }: { user: AuthUser }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  const initial = user.username.slice(0, 1).toUpperCase();

  const signOut = async () => {
    await api.logout().catch(() => undefined);
    // Force the gate to re-evaluate: /me now 401s and the login screen returns.
    await queryClient.invalidateQueries({ queryKey: ["me"] });
  };

  return (
    <div className="user-menu" ref={ref}>
      <button className="user-chip" onClick={() => setOpen((v) => !v)} aria-haspopup="menu">
        {user.thumb_url ? (
          <img src={user.thumb_url} alt="" className="user-avatar" />
        ) : (
          <span className="user-avatar user-avatar-fallback">{initial}</span>
        )}
        <span className="user-name">{user.username}</span>
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" className="chevron">
          <path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
      {open && (
        <div className="user-dropdown" role="menu">
          <div className="user-dropdown-head">
            <div className="user-name">{user.username}</div>
            <div className="muted user-provider">
              {user.provider === "plex" ? "Plex account" : "Local account"}
            </div>
          </div>
          <button className="user-dropdown-item" role="menuitem" onClick={signOut}>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

function Dashboard({ user }: { user: AuthUser }) {
  const [view, setView] = useState<View>("review");
  const [verdict, setVerdict] = useState<Verdict>("condemn");
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const { data: snapshot } = useQuery({
    queryKey: ["snapshot"],
    queryFn: api.latestSnapshot,
    // A 404 means no scan has run. That is a normal first-boot state, not an error, and
    // retrying it on a loop would be noise.
    retry: false,
  });

  const { data: detail } = useQuery({
    queryKey: ["candidate", selectedId],
    queryFn: () => api.candidate(selectedId!),
    enabled: selectedId !== null,
  });

  return (
    <div className="app">
      <header className="masthead">
        <div className="brand">
          <svg className="brand-mark sm" viewBox="0 0 48 48" fill="none" aria-hidden="true">
            <path d="M31 9C17 9 9 17 9 29c8-8 16-12 26-10-1-5-2-8-4-10Z" fill="currentColor" />
            <path d="M31 9 19 40" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" />
          </svg>
          <div className="brand-text">
            <span className="brand-word">Reaper</span>
            <span className="muted brand-sub">explainable pruning for Plex</span>
          </div>
        </div>

        <nav className="views" aria-label="Sections">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? "tab active" : "tab"}
              onClick={() => setView(n.id)}
            >
              {n.label}
            </button>
          ))}
        </nav>

        <UserMenu user={user} />
      </header>

      <SafetyBanner />
      {view === "review" && <ScanFreshness snapshot={snapshot} />}

      <main className={selectedId !== null && view === "review" ? "split" : ""}>
        {view === "review" ? (
          <>
            <ReviewQueue
              verdict={verdict}
              onVerdictChange={(v) => {
                setVerdict(v);
                setSelectedId(null);
              }}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
            {detail && <WhyPanel item={detail} onClose={() => setSelectedId(null)} />}
          </>
        ) : view === "policy" ? (
          <PolicyEditor />
        ) : view === "reap" ? (
          <ReapPlan />
        ) : view === "fairness" ? (
          <Fairness />
        ) : (
          <Settings />
        )}
      </main>
    </div>
  );
}

/** Once signed in, a fresh install goes to the setup wizard until it is configured and has
 *  scanned once; after that (or if the owner skips) it is the dashboard. */
function Authed({ user }: { user: AuthUser }) {
  const [skipped, setSkipped] = useState(false);
  const { data: setup, isLoading, isError } = useQuery({
    queryKey: ["setup"],
    queryFn: api.setupStatus,
  });

  if (isLoading) {
    return (
      <div className="auth-screen">
        <span className="spinner spinner-lg" aria-label="Loading" />
      </div>
    );
  }

  // Treat an unreadable setup status as "setup still needed": if the status call errors we
  // cannot prove the install is configured, and dropping a genuinely-fresh install onto an
  // empty Dashboard (with no way back to the wizard) is the worse failure. The owner can
  // still skip past it. Only a status we could read *and* that says complete lands on the
  // Dashboard directly.
  const needsSetup = isError || (setup !== undefined && !setup.complete);
  if (needsSetup && !skipped) {
    return <SetupWizard onSkip={() => setSkipped(true)} />;
  }
  return <Dashboard user={user} />;
}

/** The gate. Nothing renders until we know who (if anyone) is signed in. */
export function App() {
  const { data: user, isLoading, isError } = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    // A 401 is the normal logged-out state, not something to retry into a storm.
    retry: false,
    staleTime: 0,
  });

  if (isLoading) {
    return (
      <div className="auth-screen">
        <div className="auth-aurora" aria-hidden="true" />
        <span className="spinner spinner-lg" aria-label="Loading" />
      </div>
    );
  }

  if (isError || !user) return <Login />;

  return <Authed user={user} />;
}
