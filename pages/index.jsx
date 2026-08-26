import Head from "next/head";
import { useCallback, useEffect, useState } from "react";

import LeadDetail from "../components/LeadDetail";
import Sidebar from "../components/Sidebar";
import ApolloAutopilot from "../components/pages/ApolloAutopilot";
import BdrResearch from "../components/pages/BdrResearch";
import ClientExpansion from "../components/pages/ClientExpansion";
import Dashboard from "../components/pages/Dashboard";
import Engagement from "../components/pages/Engagement";
import Leads from "../components/pages/Leads";
import Prompts from "../components/pages/Prompts";
import RunPipeline from "../components/pages/RunPipeline";
import Settings from "../components/pages/Settings";
import SignalFeed from "../components/pages/SignalFeed";
import { readStoredKey, useApi, writeStoredKey } from "../lib/api";

/**
 * Cloudwork|PRO operator console.
 *
 * Statically prerendered: no getServerSideProps, no environment variables, no
 * database. Every panel fetches client-side and renders its own error state, so
 * the shell loads even when the API is down.
 *
 * The INTERNAL_API_KEY is never in this bundle — the operator pastes it in and
 * it stays in sessionStorage for the tab.
 */

export default function Home() {
  const [page, setPage] = useState("dashboard");
  const [apiKey, setApiKey] = useState("");
  const [keyInput, setKeyInput] = useState("");
  const [openLeadId, setOpenLeadId] = useState(null);
  const [prefillLead, setPrefillLead] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const stored = readStoredKey();
    setApiKey(stored);
    setKeyInput(stored);
    setReady(true);
  }, []);

  // Public endpoint: works without a key, so the shell always has something
  // true to show before the operator authenticates.
  const health = useApi("/health", null);

  const saveKey = useCallback(() => {
    writeStoredKey(keyInput.trim());
    setApiKey(keyInput.trim());
  }, [keyInput]);

  const forgetKey = useCallback(() => {
    writeStoredKey("");
    setApiKey("");
    setKeyInput("");
  }, []);

  const openLead = useCallback((id) => setOpenLeadId(id), []);

  const reprocess = useCallback((lead) => {
    setPrefillLead({
      email: lead.email,
      first_name: lead.first_name,
      last_name: lead.last_name,
      company: lead.company,
      title: lead.title,
      linkedin_url: lead.linkedin_url,
    });
    setOpenLeadId(null);
    setPage("pipeline");
  }, []);

  const healthTone =
    health.loading ? "warn" : health.data?.status === "ok" ? "ok" : "err";
  const healthLabel =
    health.loading ? "checking API…"
      : health.data?.status === "ok" ? "API ok"
        : health.data?.status === "degraded" ? "API degraded"
          : "API unavailable";

  const shared = { apiKey, onOpenLead: openLead };

  return (
    <>
      <Head>
        <title>Cloudwork|PRO — Sales Enablement</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
      </Head>

      <div className="layout">
        <Sidebar
          current={page}
          onNavigate={setPage}
          apiKey={apiKey}
          onForgetKey={forgetKey}
        />

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
                Lead data requires <code>INTERNAL_API_KEY</code>. It is not stored
                in this deployment — it stays in this browser tab and is sent
                directly to the same-origin API. Health status works without it.
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

          <main>
            {page === "dashboard" && (
              <Dashboard {...shared} health={health} onNavigate={setPage} />
            )}
            {page === "signals" && <SignalFeed {...shared} />}
            {page === "expansion" && <ClientExpansion {...shared} />}
            {page === "leads" && <Leads {...shared} />}
            {page === "pipeline" && (
              <RunPipeline
                apiKey={apiKey}
                prefillLead={prefillLead}
                onPrefillConsumed={() => setPrefillLead(null)}
              />
            )}
            {page === "apollo" && <ApolloAutopilot />}
            {page === "settings" && <Settings apiKey={apiKey} health={health} />}
            {page === "engagement" && <Engagement {...shared} />}
            {page === "prompts" && <Prompts apiKey={apiKey} />}
            {page === "research" && <BdrResearch {...shared} />}
          </main>
        </div>

        {openLeadId !== null && (
          <LeadDetail
            leadId={openLeadId}
            apiKey={apiKey}
            onClose={() => setOpenLeadId(null)}
            onAction={reprocess}
          />
        )}
      </div>
    </>
  );
}
