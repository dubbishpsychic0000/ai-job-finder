# Project status

## Current state

The discovery pipeline has connector-level diagnostics, deduplication, safety
gates, public ATS connectors, and an index-only LinkedIn path. Search-provider
failures are isolated from the rest of the pipeline.

The current hardening pass addresses silent zero-result behavior, Tavily quota
waste, unsafe email provenance, and accidental CI cache/budget resets. Search
coverage has not yet been measured against a representative employer set.

## Phase 1 implemented

`app.discovery.website_researcher.WebsiteResearcher` now provides a bounded,
incremental, same-site crawl starting from an official or discovered URL. It
checks robots rules, paces requests per host, deduplicates normalized URLs,
prioritizes careers/recruitment/contact/spontaneous pages, reads XML sitemaps,
records linked public PDFs, extracts published emails and application
instructions, and persists each fact in the `evidence` table with URL, page
title, snippet, relationship, confidence, and reason code. Existing companies
gain `official_domain`; the existing discovery workflow can opt in with
`discovery.website_research` and projects evidence-backed routes into company
and job rows.

Email classification distinguishes employer recruitment/general addresses,
ATS vendors, free-mail, unrelated third-party, and unsupported values. A
small `evidence_backed_fields` guard is available for any future LLM
extraction: values absent from captured evidence are discarded.

Measured validation: the focused website-research tests cover sitemap
discovery, prioritization, deduplication, limits, robots handling, email
classification, evidence persistence, spontaneous applications, incremental
research, and evidence-backed extraction. The full suite remains green at
**218 passed, 1 skipped**.

## Phase 2 implemented

The discovery intake is now broader than conventional jobs. A persistent
`discoveries` ledger stores `JOB`, `RECRUITMENT_POST`, `COMPANY`, and
`CAREER_PAGE` records with canonical deduplication, discovery channel,
source type, evidence, reason, relevance, and research status. Existing
opportunity and social workflows write to this ledger without changing the
matching or safety pipeline.

The LinkedIn connector now searches public indexes for both `/jobs` and
`/posts`. Indexed posts are classified as `RECRUITMENT_POST` and retain their
snippet as unverified evidence; the connector still never requests LinkedIn
directly.

`OpenCompanyDiscovery` adds bounded, injectable adapters for OSM/Overpass and
Wikidata. These produce vacancy-independent company candidates and persist
canonical companies with official domains, industry, discovery reason, and
relevance. The company-universe workflow can hand candidates to the Phase 1
Website Researcher.

Focused Phase 2 validation covers indexed recruitment posts, open company
adapters, canonical deduplication, provenance persistence, and no-vacancy
company ingestion.

## Remaining / unverified

PDF text extraction, structured JSON-LD job extraction, sitemap index
recursion, JavaScript-rendered pages, DNS/MX checks, and broad coverage across
real employers remain unverified. The researcher intentionally does not bypass
CAPTCHAs or protected pages. These should be measured against a golden set
before enabling website research broadly in scheduled runs.

The OSM/Wikidata adapters are implemented but not enabled by default. Their
real-world yield, query limits, entity quality, and local Moroccan coverage
still require a bounded pilot. Common Crawl URL seeding, procurement and
professional-directory adapters, and a measured LinkedIn indexed-search pilot
remain future work.
