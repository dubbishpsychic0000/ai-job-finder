# Project status

## Production Gmail draft validation

The saved Gmail token at the private vault's `secrets/gmail_token.json` passed
a forced in-memory refresh and includes the Gmail compose scope. The Actions
workflow restores `candidate/` and `secrets/` from that same vault before
running the selected task. The candidate profile and French CV were present at
their expected vault paths. The manual run used `task=btp_outreach`,
`email_mode=draft`, `authorize_live_btp=false`, and `max_companies=1`;
scheduled runs remain draft-only.

Actions run **38207900891** on public-repository commit `cb34fa5` selected
Senan Holding. Its official careers page publishes
`careers@senanholding.com` and spontaneous-application instructions; persisted
verification and evidence records passed the recruitment-contact checks. The
application safety gate passed, and the French CV was attached. Gmail returned
a real draft ID, which was persisted in the application database and matched
the draft fetched from Gmail. The live draft was inspected: one draft exists for
the intended recipient and subject, the message is the expected generic French
spontaneous-application text, and exactly one non-empty
French CV attachment is present. Actions state persistence completed
successfully.

Measured result: **1 draft attempt, 1 confirmed Gmail draft, 0 sends**. No
second production run was made, so duplicate suppression after a real draft has
not yet been production-tested; avoiding an accidental second draft took
priority here. No live-send path was enabled or attempted. Earlier runs
**38190098129** and **38192852330** failed with OAuth refresh errors before
Gmail draft creation; the latter's failed attempt remained retryable without
weakening duplicate protection.

## Draft duplicate-suppression and recovery audit

The application workflow already blocks a second action for a job with a
persisted `drafted`, `sent`, or `deferred` application; the BTP company workflow
also recognizes existing applications and its persisted screening record.
Regression tests now reload the DB in a separate SQLAlchemy session and confirm
the same job is suppressed while a different employer can still receive its
own draft.

Gmail draft creation now uses a durable recovery intent. Before calling Gmail,
the workflow commits the application/email attempt and a job-scoped
`draft_attempt_key`; that opaque key is also placed in an
`X-WCA-Draft-Attempt` MIME header. A partial unique DB index reserves that key
for creating, uncertain, and confirmed attempts, preventing competing workers
from committing separate active intents for the same job. Known pre-create
failures move to `not_attempted`, allowing a safe later retry. A successful
Gmail response and persisted ID are recorded as `confirmed`; timeouts, missing
IDs, and other uncertain create outcomes remain `uncertain`/`deferred`.

The custom MIME header is an app-defined reconciliation hint, not a Gmail
idempotency feature, and its preservation by real Gmail has **not** been
verified. Recovery scans a bounded, paginated set of drafts and adopts exactly
one full-response match. Multiple matches, scan limits, malformed/partial
responses, API failures, and no marker match all remain
`recovery_required`; importantly, `not_found` means only “no matching marker in
a complete scan,” not proof that Gmail did not create the draft. No such result
permits automatic recreation. The BTP report likewise surfaces
`draft_recovery_required`.

Hermetic regression coverage exercises confirmed-draft suppression after DB
reload, repeat BTP execution, API ambiguity, marker-based recovery after a
simulated persistence failure, no-match fail-closed behavior, distinct
employers, pagination, multiple matches, incomplete scans, partial responses,
missing markers, initial-intent commit failure, a competing worker rejected by
the database uniqueness guard, and the existing dry-run/draft/live
authorization gates.
Company-universe research now spends its bounded per-run company budget on due
companies first, prioritizing companies without a qualifying contact and
rotating toward never-researched or older candidates rather than repeatedly
selecting only the highest-relevance company. Tests compare the previous
relevance-first selection with the updated policy on the same synthetic
fixture: the previous policy used **0 research requests**, found **0 new
companies**, and qualified **0 contacts**; the updated policy used **1
request**, researched **1 company for the first time**, qualified **1
contact**, and repeated **0 companies**. A four-run budget-one fixture
researched **3 unique companies**, all **3 for the first time**, then revisited
the oldest candidate once; total requests were **4** (never more than the
per-run budget). A separate quality fixture retained **2 qualifying contacts
and 1 outreach-ready company**, prioritized a no-contact candidate over a
higher-relevance stale-contact candidate, and excluded a below-threshold
candidate.

No production workflow or Gmail mutation was run for these measurements; they
are deterministic fixture results, not expected production outcomes.
Final safety-review validation: targeted recovery/discovery tests **142 passed,
1 skipped**; full suite **348 passed, 1 skipped**; changed-file Ruff and
`git diff --check` passed. Gmail custom-header preservation and duplicate
suppression remain unverified against production; if the API does not preserve
the marker, recovery fails closed for manual reconciliation rather than
blindly retrying.

## Recruitment contact qualification fix

Website page classification now uses URL, title, and headings rather than
incidental keywords in page body text. The crawler prioritizes direct
careers/contact navigation, filters and bounds sitemap seeds, and records
explicit application instructions separately from page type. Career-position
headings and email addresses followed by sentence punctuation are extracted.

An alternate recruitment email domain is accepted only when the address,
application instruction, and HTTPS page all come from the same established
employer domain and the instruction directly names that address. ATS and
free-mail domains remain rejected. The evidence ledger records the
`official_alternate_recruitment_domain` relationship and the verifier records
`official_page_published_alternate_recruitment_domain`; both the source URL and
instruction excerpt are included in per-company results. Ciments du Maroc and
Senan Holding have page-level regression fixtures. A bounded public
Senan Holding careers seed is preserved when Actions restores an older private
vault candidate directory, and screening policy version 4 permits old negative
screenings to be re-evaluated without overriding existing applications/drafts.

Measured validation before production dispatch: **313 passed, 1 skipped**;
changed-file Ruff and `git diff --check` passed. Fixture tests do not establish
production Gmail delivery; that result is recorded below after the real
draft-only run.

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
