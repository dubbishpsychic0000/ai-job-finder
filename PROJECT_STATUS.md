# Project status

## Local discovery and outreach safety hardening

Spontaneous BTP outreach now requires explicit, same-page application or CV
submission instructions that name the selected email address. A published
recruitment address by itself, a configured general-contact fallback, or an
application link without same-page instructions cannot authorize a draft.
Official application portals remain reportable as form routes, but only when
the employer page also publishes actionable submission instructions. Per-company
reason codes distinguish missing instructions, incomplete crawls, mismatched
contact routes, and confirmed portal instructions.

Company-universe research distinguishes a verified published recruitment
contact from an actionable outreach route. A company with an address but no
application instructions is not marked outreach-ready and remains due for
research; companies without verified contacts retain first priority.

Tavily request slots are durably reserved before provider dispatch. Reservations
share a cross-process locked UTC-day ledger per key, so retries and failed
requests consume the same limits as successful calls. Duplicate configured
keys are collapsed. General discovery has a shared aggregate cap of 17 provider
attempts per run, including retries, while preserving the separate three-call
daily BTP reserve. Cache responses expire at the 30-day query-response TTL;
legacy entries without timestamps are stale. Discovery reports distinguish
API attempts, successes, failed attempts, retries, cache hits, empty results,
budget deferrals, normalization rejections, duplicate classes, and new jobs.
The cache TTL does not establish the age or availability of job postings
contained in a cached response.

Validation uses deterministic fixtures and mocked providers only: the focused
discovery/BTP/Gmail/company-universe set passed **167 tests, 1 skipped**; the
full suite passed **370 tests, 1 skipped**; Ruff passed on all changed Python
files; and `git diff --check` passed. No live Tavily request, website crawl,
Gmail operation, production workflow, commit, or push was performed for this
hardening pass.

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

The BTP CLI launch gate has been corrected: `app/cli.py` now allows the manual
`EMAIL_MODE=live` path only when `ENABLE_EMAIL=true`, `EMAIL_PROVIDER=gmail`, and
`AUTHORIZE_LIVE_SPONTANEOUS=true`. Scheduled runs remain draft-only because the
workflow forces `EMAIL_MODE=draft` for `schedule` events and only populates the
live authorization flag in the manual `btp_outreach` dispatch path. This aligns
with the existing workflow and keeps the safety gate in the service layer.

Live Gmail sending requires `gmail.send` and `gmail.compose`. The broader
`gmail.readonly` scope is optional and only enables automatic Sent-mail
reconciliation after an uncertain send. Without it, an uncertain attempt stays
persisted and blocked for operator review; it is never retried automatically.
`gmail-status` reports required send scopes separately from the optional
reconciliation scope and whether that optional grant is present.

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

## BTP contact-route diagnosis

The artifact for BTP run **38214738802** recorded Ciments du Maroc's
`careers_url` and `recruitment_url` as the unrelated Quarry Life Award page.
The workflow previously trusted those persisted route fields before the
verified employer homepage, so stale route metadata could direct a bounded
crawl away from current contact/career navigation. That is the concrete
pipeline defect established by the run artifact; it does not establish whether
the recruitment address was still published on the site at run time.

BTP website research now starts from the verified employer website, allowing
the bounded crawler to follow current navigation and sitemap links instead of
assuming stored route metadata is current. When a crawled page contains
explicit application instructions, its evidence-backed URL replaces the
stored career/recruitment route fields (including stale values) in both
company state and the current report candidate. A regression seeds the Ciments
company with the stale award route, keeps that route in the sitemap, and
verifies discovery through the homepage's contact navigation and replacement
with the actual instruction page.

This is a fixture-level reproduction and fix, not a new production crawl or
Gmail operation. The historical artifact does not contain page-level fetch and
extraction evidence sufficient to prove every stage of the prior run. Scheduled
runs **38319771898** and **38270366253** were separately blocked upstream by
having no eligible job target; this route fix does not address that condition.
Validation: full test suite **348 passed, 1 skipped**; changed-file Ruff and
`git diff --check` passed.

## Scheduled zero-job discovery diagnosis

Scheduled runs **38270366253** and **38319771898** did execute the configured
job-discovery stage. Run **38319771898** queried 21 search-plan entries per
configured connector. The broad Tavily connector returned 76 normalized
opportunities, all deduplicated (76 duplicates, 0 new jobs); the remaining
enabled Tavily connectors returned 0. Analysis correctly examined 0 jobs
because it only processes newly stored jobs. Discovery and analysis reported
no connector errors, so the old artifact alone made an exhausted/failing
provider look like an ordinary empty result.

The logs contain 236 “all Tavily keys spent/failed” messages for
**38319771898** and 247 for **38270366253**. The latter run's artifact recorded
86 normalized opportunities, all duplicates, plus one successful HTTP 200
Tavily diagnostic; its other uncached searches had no successful provider
response. The logs conflate exhausted local request budgets with provider
failures, so they do not establish whether the persisted key budget, provider
quota, credentials, or network caused the unavailable searches. The latest
run's 76 results came from prior cached query responses; no live Tavily
success was recorded in its debug artifact.

The configured search plan allowed 21–22 queries to run against each of 12
enabled Tavily connectors, with no aggregate per-run cap (`max_requests_per_source`
defaults to unlimited). The shared general-search budget is only 17 requests
per key per UTC day (20 total, with 3 reserved for BTP). Consequently the
first connector could consume the usable budget before domain-restricted
sources were reached; cached results also had no expiration and could be
reused indefinitely. Company discovery remained separate: the latest run's
BTP stage processed 9 of 18 candidates and researched 3, but made no draft
attempts. Contact, instruction, identity, and duplicate safeguards were not
relaxed.

The search wrapper now reports whether results came from cache or the
provider, and emits safe reason codes for budget exhaustion, provider quota,
and provider request failure. Tavily query results expire after 30 days;
expired entries are not passed downstream as current job evidence. Uncached
queries are allocated round-robin across configured budgeted connectors using
the shared remaining API budget, with the existing per-source query cap
preserved. The scheduler skips uncached Tavily requests after budget
exhaustion and reports per-source empty, failed, cached, stale-cache,
budget-deferred, normalization-rejected, and exact/probable/in-run duplicate
counts. A zero-new-job run with fetched duplicates now has an explicit
actionable alert. Company-level BTP research continues independently of job
discovery.

All changes are fixture/mock validated only. No live discovery, website crawl,
Gmail operation, workflow dispatch, credential change, commit, or push was
performed. External Tavily availability and actual discovery yield after this
change remain unverified. Focused recovery/discovery/company-pipeline/BTP
tests: **115 passed**; full suite: **354 passed, 1 skipped**. Ruff passed on
all changed Python files and `git diff --check` passed. The patch is unstaged
on `main`; no untracked files were created.
