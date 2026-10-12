# Project status

## Current delivery and persistence hardening

The BTP path now keeps the configured `dry_run`, `draft`, and `live` modes
consistent through `ApplicationEngine`. Spontaneous live delivery is default
off and requires an explicit authorization setting; authorized BTP live runs
are capped at one company. Successful sends require a provider message ID, and
Gmail drafts count as confirmed only when Gmail returns a draft ID.

GitHub Actions preserves the manually selected email mode, forces scheduled
runs to draft mode, and persists the SQLite database without deleting its
company/application/evidence records. The database WAL is checkpointed before
the vault snapshot. Website research no longer treats visit timestamps as
evidence: a previously visited page is fetched again when its corresponding
database evidence is missing. Negative screening records are policy-versioned;
confirmed drafts and sends remain duplicate-protected.

BTP company reports now expose per-company identity rejection reasons,
recruitment/contact provenance, contact confidence, delivery stage, and
previous-application/draft/cooldown eligibility flags. Provider failures,
missing CVs, safety-gate blocks, crawl failures, and unconfirmed drafts are
distinguished rather than reported as successful delivery. Official website
identity checks use exact domain-label/name token matching or significant
company-name overlap; unrelated third-party URLs remain provenance only.

Measured validation after these changes: **306 passed, 1 skipped** in the full
suite; focused BTP/email/research/discovery coverage passed
(**102 passed, 1 skipped**);
changed-file Ruff and `git diff --check` passed.

Two production draft-only Actions runs completed successfully on commit
`adb6cf6` (`38188088929`, then `38188250611`). The first found 17 candidates,
2 known website candidates, and researched 2; 8 had no verified official
website, 1 had no explicit spontaneous instructions, and 1 had no qualifying
email. It recorded 1 crawl failure, 4 rejected third-party/mismatched website
URLs, and no search-budget deferrals. It attempted **0** drafts and **0** sends
because no target qualified. The state snapshot was pushed to the private
vault as `1bf8ab2`.

The second run restored state from that vault, reported 17 company-pool
candidates and 4 existing/screened companies, and again made **0** draft or
send attempts; it reported no eligible production smoke target. Its updated
state snapshot was also pushed to the vault. This confirms cross-run state
restoration and screening, but not duplicate suppression of a real Gmail
draft: no legitimate draft was created. Gmail draft transport and live
delivery therefore remain unverified. Live authorization was false in both
runs, and **no email was sent**.

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

At the Phase 1 baseline, focused website-research tests covered sitemap
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

The OSM/Wikidata adapters are enabled through bounded company-universe
configuration. Their real-world yield, query limits, entity quality, and local
Moroccan coverage still require a bounded production pilot. Common Crawl URL
seeding, procurement and professional-directory adapters, and a measured
LinkedIn indexed-search pilot remain future work.
