import { Card, KV, PageHead, Section } from "../common";

/**
 * Status page for future automated sourcing.
 *
 * UI-only on purpose: there is no Apollo integration in the backend, so nothing
 * here reads or writes. It states that plainly rather than showing controls that
 * would do nothing.
 */
export default function ApolloAutopilot() {
  return (
    <>
      <PageHead
        title="Apollo Autopilot"
        note="Automated weekly lead sourcing. Not connected yet."
      />

      <Section title="Status">
        <Card>
          <p className="state">
            Apollo Autopilot is off. Enable it to source weekly lead batches.
          </p>
          <p className="hint">
            This page is a placeholder. There is no Apollo integration in the
            backend, so nothing here is wired up — the controls below are
            disabled rather than pretending to work.
          </p>
        </Card>
      </Section>

      <Section title="Planned configuration">
        <Card>
          <KV rows={[
            ["Status", <span className="pill-sm">disabled</span>],
            ["Cadence", "weekly"],
            ["Batch size", "—"],
            ["Search filters", "—"],
            ["Last run", "never"],
            ["Next run", "—"],
          ]} />
          <div className="row">
            <button type="button" disabled>Enable Autopilot</button>
            <button type="button" className="ghost" disabled>Configure search</button>
          </div>
        </Card>
      </Section>

      <Section title="How leads arrive today">
        <Card>
          <ul className="plain">
            <li>
              <span className="method">POST</span>
              <code>/api/v1/leads/process</code> — submit a batch from Run Pipeline
              or any caller with the internal API key.
            </li>
            <li>
              <span className="method">POST</span>
              <code>/api/lead-source/run-scheduled</code> — the evergreen
              lead-source import, triggered by an external scheduler.
            </li>
            <li>
              <span className="method">CSV</span>
              Bulk import through the ingest scripts.
            </li>
          </ul>
        </Card>
      </Section>
    </>
  );
}
