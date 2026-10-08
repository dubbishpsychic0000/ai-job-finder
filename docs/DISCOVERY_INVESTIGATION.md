# Discovery investigation

This repository uses search providers as discovery inputs, not as the system
of record. Public ATS feeds and the employer's own public pages remain the
preferred evidence sources.

## Hardening delivered

- LinkedIn search-index results are retained as unverified evidence while the
  connector continues to avoid direct requests to `linkedin.com`.
- Tavily successful empty responses are cached and charged once, so a negative
  query is not retried against every rotated key.
- Tavily defaults to `basic` search depth. Set `TAVILY_SEARCH_DEPTH` explicitly
  when a different provider mode is justified.
- Discovery reports now expose per-source health (`ok`, `empty`,
  `unconfigured`, or `degraded`) and raise a warning alert when every source
  returns zero opportunities.
- Official email verification requires the address domain to match a known
  employer domain; an ATS vendor URL alone is not enough to establish that
  relationship. ATS vendor and free-mail addresses remain unverified.
- CI preserves Tavily and LLM budget/cache state between vault-backed runs.

## Implemented research and company seeds

`WebsiteResearcher` and the evidence store are implemented. The bounded crawler
honors robots rules, records public-page evidence, and persists successful
fetches with timestamps so failed fetches can be retried and successful pages
can be refreshed after `discovery.website_research_refresh_days` (7 days by
default). Legacy URL-only state is treated as expired and crawled again.

`OpenCompanyDiscovery` has OSM/Overpass and Wikidata adapters. The
`discovery.company_universe_discovery` flag wires these sources into the
discovery workflow. Configure `discovery.company_universe_sources` as a list:

```yaml
company_universe_sources:
  - kind: osm
    overpass_url: "https://overpass-api.de/api/interpreter"
    query: "<bounded Overpass QL query>"
    country: "Morocco"
    target_terms: ["engineering", "construction"]
  - kind: wikidata
    endpoint: "https://query.wikidata.org/sparql"
    sparql: "<bounded SPARQL query>"
    country: "Morocco"
    target_terms: ["engineering"]
company_universe_max_candidates: 100
company_universe_min_relevance: 0
```

Source queries are externally configured; the workflow limits source entries
to 10 and candidates to 500 maximum, and reports source/persistence errors in
the discovery report. Keep the feature disabled until bounded queries are
configured and their yield has been piloted.

## Remaining investigation

Structured JSON-LD job extraction, sitemap index recursion, JavaScript-rendered
pages, DNS/MX checks, and broad coverage across real employers remain
unverified. The researcher intentionally does not bypass CAPTCHAs or protected
pages. Coverage and adapter yield should be measured against a golden set
before enabling research broadly in scheduled runs.
