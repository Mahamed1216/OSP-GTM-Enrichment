import { formatDate, useApi } from "../../lib/api";
import { AsyncState, Bool, Card, Metric, PageHead, Section } from "../common";

export default function BdrResearch({ authed, onOpenLead }) {
  const { data, loading, error, reload } = useApi(
    "/api/v1/research?limit=50",
    { skip: !authed },
  );
  const items = data?.research || [];

  const withBuyer = items.filter((item) => item.has_buyer_research).length;
  const withNews = items.filter((item) => item.has_company_news).length;
  const withProfile = items.filter((item) => item.has_profile).length;

  return (
    <>
      <PageHead
        title="BDR Research"
        note="What the enrichment waterfall found per lead: profile, company, news and buyer research."
      />

      <Section title="Coverage" note="Across the most recent enrichment runs.">
        <div className="metrics">
          <Metric label="Enriched leads" value={items.length} accent />
          <Metric label="LinkedIn profile" value={withProfile} />
          <Metric label="Company news" value={withNews} />
          <Metric label="Buyer research" value={withBuyer} />
        </div>
      </Section>

      <Section
        title="Research output"
        actions={<button type="button" className="ghost" onClick={reload}>Refresh</button>}
      >
        <Card>
          <AsyncState
            loading={loading}
            error={error}
            empty={!loading && items.length === 0}
            emptyTitle="No research yet"
            emptyText="Enrichment output appears here after a pipeline run."
          >
            <div className="content-list">
              {items.map((item) => (
                <article key={item.lead_id} className="content-card">
                  <div className="content-meta">
                    <button type="button" className="linkish" onClick={() => onOpenLead(item.lead_id)}>
                      {item.lead_name || `Lead #${item.lead_id}`}
                    </button>
                    {item.company && <span className="muted">· {item.company}</span>}
                    {item.company_domain && <span className="muted mono">{item.company_domain}</span>}
                    <span className="spacer" />
                    <span className="muted">{formatDate(item.enriched_at)}</span>
                  </div>

                  <ul className="chips">
                    <li>profile <Bool value={item.has_profile} yes="✓" no="—" /></li>
                    <li>company <Bool value={item.has_company} yes="✓" no="—" /></li>
                    <li>company news <Bool value={item.has_company_news} yes="✓" no="—" /></li>
                    <li>industry news <Bool value={item.has_industry_news} yes="✓" no="—" /></li>
                    <li>buyer research <Bool value={item.has_buyer_research} yes="✓" no="—" /></li>
                  </ul>

                  {item.news_headlines.length > 0 && (
                    <>
                      <p className="hint" style={{ marginTop: ".6rem" }}>Recent headlines</p>
                      <ul className="plain">
                        {item.news_headlines.map((headline, i) => <li key={i}>{headline}</li>)}
                      </ul>
                    </>
                  )}

                  {item.buyer_segments.length > 0 && (
                    <>
                      <p className="hint" style={{ marginTop: ".6rem" }}>Buyer segments</p>
                      <ul className="chips">
                        {item.buyer_segments.map((segment, i) => <li key={i}>{segment}</li>)}
                      </ul>
                    </>
                  )}
                </article>
              ))}
            </div>
          </AsyncState>
        </Card>
      </Section>
    </>
  );
}
