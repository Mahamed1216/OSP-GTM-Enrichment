import Head from "next/head";
import { useCallback, useEffect, useState } from "react";

import Content from "../components/Content";
import LeadDetail from "../components/LeadDetail";
import Leads from "../components/Leads";
import Overview from "../components/Overview";
import Processing from "../components/Processing";
import Settings from "../components/Settings";
import { readStoredKey, useApi, writeStoredKey } from "../lib/api";

/**
 * Operator console for the standalone GTM enrichment app.
 *
 * Statically prerendered: no getServerSideProps, no environment variables, no
 * database. Every panel fetches client-side and renders its own error state, so
 * the page loads even when the API is down.
 *
 * The INTERNAL_API_KEY is never in this bundle. The operator pastes it in and
 * it stays in sessionStorage for the tab.
 */

const TABS = [
  ["overview", "Overview"],
  ["leads", "Leads"],
  ["content", "Content"],
  ["processing", "Processing"],
  ["settings", "Settings"],
];

export default function Home() {
  const [tab, setTab] = useState("overview");
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

  // Public endpoint: works with no key, so the shell always has something true
  // to show even before the operator authenticates.
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
    setTab("processing");
  }, []);

  const healthTone =
    health.loading ? "warn" : health.data?.status === "ok" ? "ok" : "err";

  return (
    <>
      <Head>
        <title>OSP GTM Enrichment — Operator Console</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="robots" content="noindex" />
      </Head>

      <div className="shell">
        <header className="topbar">
          <div className="brand">
            <h1>OSP GTM Enrichment</h1>
            <span className="marker">REAL STANDALONE VERCEL UI LOADED</span>
          </div>
          <span className={`pill ${healthTone}`}>
            <span className={`dot ${healthTone}`} />
            {health.loading
              ? "checking API…"
              : health.data?.status === "ok"
                ? "API ok"
                : health.data?.status === "degraded"
                  ? "API degraded"
                  : "API unavailable"}
          </span>
        </header>

        <nav className="tabs">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={tab === id ? "tab active" : "tab"}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>

        {ready && !apiKey && (
          <section className="card span keygate">
            <h2>Enter the internal API key</h2>
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
          </section>
        )}

        <main>
          {tab === "overview" && (
            <Overview apiKey={apiKey} health={health} onOpenLead={openLead} />
          )}
          {tab === "leads" && <Leads apiKey={apiKey} onOpenLead={openLead} />}
          {tab === "content" && <Content apiKey={apiKey} onOpenLead={openLead} />}
          {tab === "processing" && (
            <Processing
              apiKey={apiKey}
              prefillLead={prefillLead}
              onPrefillConsumed={() => setPrefillLead(null)}
            />
          )}
          {tab === "settings" && <Settings apiKey={apiKey} health={health} />}
        </main>

        {openLeadId !== null && (
          <LeadDetail
            leadId={openLeadId}
            apiKey={apiKey}
            onClose={() => setOpenLeadId(null)}
            onAction={reprocess}
          />
        )}

        <footer className="foot">
          <span className="muted">
            API endpoints stay available under <code>/api</code> —{" "}
            <code>/health</code> and <code>/api/info</code> are public.
          </span>
          {apiKey && (
            <button type="button" className="linkish" onClick={forgetKey}>
              Forget API key
            </button>
          )}
        </footer>
      </div>
    </>
  );
}
