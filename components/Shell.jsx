import Head from "next/head";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

import LeadDetail from "./LeadDetail";
import Sidebar from "./Sidebar";
import { useApi, useStoredKey, writeStoredKey } from "../lib/api";

/**
 * Console layout shared by every route: sidebar, health pill, API-key gate and
 * the lead drawer.
 *
 * Pages read `apiKey` and `openLead` from context rather than prop-drilling
 * through ten route files. Nothing here is server-rendered — every route stays
 * statically prerenderable.
 */

const ConsoleContext = createContext({
  apiKey: "",
  openLead: () => {},
  health: { loading: true, data: null, error: null },
});

export function useConsole() {
  return useContext(ConsoleContext);
}

/** Hand a lead to /run-pipeline across a real route change. */
export const PREFILL_STORE = "signalos.prefillLead";

export default function Shell({ title, children }) {
  const apiKey = useStoredKey();
  const [keyInput, setKeyInput] = useState("");
  const [openLeadId, setOpenLeadId] = useState(null);
  const [ready, setReady] = useState(false);

  // Only used to avoid flashing the key gate during hydration.
  useEffect(() => setReady(true), []);

  // Public endpoint: works without a key, so the shell always has something
  // true to show before the operator authenticates.
  const health = useApi("/health", null);

  const saveKey = useCallback(() => {
    writeStoredKey(keyInput.trim());
  }, [keyInput]);

  const forgetKey = useCallback(() => {
    writeStoredKey("");
    setKeyInput("");
  }, []);

  const openLead = useCallback((id) => setOpenLeadId(id), []);

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
    <ConsoleContext.Provider value={{ apiKey, openLead, health }}>
      <Head>
        <title>{title ? `${title} · SignalOS` : "SignalOS — Sales Enablement"}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
      </Head>

      <div className="layout">
        <Sidebar apiKey={apiKey} onForgetKey={forgetKey} />

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

          {ready && !apiKey && (
            <div className="card" style={{ marginBottom: "1.75rem" }}>
              <h3>Enter the internal API key</h3>
              <p className="hint">
                SignalOS data requires <code>INTERNAL_API_KEY</code>. It is not
                stored in this deployment — it stays in this browser tab and is
                sent directly to the same-origin API. Health status works
                without it.
              </p>
              <div className="row">
                <div className="grow">
                  <input
                    type="password"
                    value={keyInput}
                    placeholder="paste the internal API key"
                    autoComplete="off"
                    onChange={(e) => setKeyInput(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && saveKey()}
                  />
                </div>
                <button type="button" onClick={saveKey} disabled={!keyInput.trim()}>
                  Unlock
                </button>
              </div>
            </div>
          )}

          <main>{children}</main>
        </div>

        {openLeadId !== null && (
          <LeadDetail
            leadId={openLeadId}
            apiKey={apiKey}
            onClose={() => setOpenLeadId(null)}
            onAction={handoffToPipeline}
          />
        )}
      </div>
    </ConsoleContext.Provider>
  );
}
