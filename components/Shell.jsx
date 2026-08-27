import Head from "next/head";
import { createContext, useCallback, useContext, useState } from "react";

import LeadDetail from "./LeadDetail";
import Sidebar from "./Sidebar";
import { login as apiLogin, logout as apiLogout, useApi } from "../lib/api";

/**
 * Console layout shared by every route: sidebar, health pill, admin login and
 * the lead drawer.
 *
 * Auth is an HttpOnly session cookie issued by POST /api/auth/login against
 * ADMIN_PASSWORD. Nothing sensitive lives in the browser: no credential in this
 * bundle, nothing in localStorage or sessionStorage, and INTERNAL_API_KEY never
 * reaches the client at all — it stays server-side for backend callers.
 *
 * Pages read `authed` from context rather than prop-drilling through ten route
 * files. Every route stays statically prerenderable.
 */

const ConsoleContext = createContext({
  authed: false,
  openLead: () => {},
  health: { loading: true, data: null, error: null },
});

export function useConsole() {
  return useContext(ConsoleContext);
}

/** Hand a lead to /run-pipeline across a real route change. */
export const PREFILL_STORE = "signalos.prefillLead";

export default function Shell({ title, children }) {
  const [openLeadId, setOpenLeadId] = useState(null);

  // Public endpoints: both work signed out, so the shell always has something
  // true to show.
  const health = useApi("/health");
  const session = useApi("/api/auth/me");

  const authed = session.data?.authenticated === true;
  const loginConfigured = session.data?.login_configured !== false;

  const openLead = useCallback((id) => setOpenLeadId(id), []);

  const signOut = useCallback(async () => {
    await apiLogout();
    session.reload();
  }, [session]);

  const handoffToPipeline = useCallback((lead) => {
    try {
      window.sessionStorage.setItem(
        PREFILL_STORE,
        JSON.stringify({
          email: lead.email,
          first_name: lead.first_name,
          last_name: lead.last_name,
          company: lead.company,
          title: lead.title,
          linkedin_url: lead.linkedin_url,
        }),
      );
    } catch {
      /* private mode — the pipeline page just starts from the sample payload */
    }
    setOpenLeadId(null);
    window.location.href = "/run-pipeline";
  }, []);

  const healthTone =
    health.loading ? "warn" : health.data?.status === "ok" ? "ok" : "err";
  const healthLabel =
    health.loading ? "checking API…"
      : health.data?.status === "ok" ? "API ok"
        : health.data?.status === "degraded" ? "API degraded"
          : "API unavailable";

  return (
    <ConsoleContext.Provider value={{ authed, openLead, health }}>
      <Head>
        <title>{title ? `${title} · SignalOS` : "SignalOS — Sales Enablement"}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
      </Head>

      <div className="layout">
        <Sidebar authed={authed} onSignOut={signOut} />

        <div className="content">
          <div className="topline">
            <span className="marker">
              <span className="dot" />
              REAL STANDALONE VERCEL UI LOADED
            </span>
            <span className="pill">
              <span className={`dot ${healthTone}`} />
              {healthLabel}
            </span>
          </div>

          {!session.loading && !authed && (
            <AdminLogin
              configured={loginConfigured}
              onSignedIn={session.reload}
            />
          )}

          <main>{children}</main>
        </div>

        {openLeadId !== null && (
          <LeadDetail
            leadId={openLeadId}
            authed={authed}
            onClose={() => setOpenLeadId(null)}
            onAction={handoffToPipeline}
          />
        )}
      </div>
    </ConsoleContext.Provider>
  );
}

/** Password form. The password is posted and never stored client-side. */
function AdminLogin({ configured, onSignedIn }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(event) {
    event.preventDefault();
    if (!password) return;
    setBusy(true);
    setError(null);
    const result = await apiLogin(password);
    setBusy(false);
    setPassword(""); // never keep it around, even in component state
    if (result.ok) {
      onSignedIn();
    } else {
      setError(
        result.status === 401
          ? "Incorrect password."
          : result.error || "Sign-in failed.",
      );
    }
  }

  return (
    <div className="card" style={{ marginBottom: "1.75rem" }}>
      <h3>Admin login</h3>
      <p className="hint">
        Enter the admin password to access SignalOS data and actions. Health
        status works without signing in.
      </p>

      {!configured && (
        <p className="state err">
          No admin password is configured on the server. Set{" "}
          <code>ADMIN_PASSWORD</code> in the deployment environment and redeploy.
        </p>
      )}

      <form onSubmit={submit}>
        <div className="row">
          <div className="grow">
            <label htmlFor="admin-password">Password</label>
            <input
              id="admin-password"
              type="password"
              value={password}
              placeholder="admin password"
              autoComplete="current-password"
              disabled={!configured || busy}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button type="submit" disabled={!configured || busy || !password}>
            {busy ? "Signing in…" : "Log in"}
          </button>
        </div>
      </form>

      {error && <p className="state err">{error}</p>}
    </div>
  );
}
