# Project status

## Phase 2 multilingual discovery and internship eligibility

Implementation commit: `a3dd154d04394b41956cea05545f01b3104f2ab2`.

Discovery now plans French and Arabic searches for Morocco and country-relevant
language coverage for the configured European and Canadian markets. Curated
localized civil-engineering/BTP terms and internship/PFE queries supplement
the existing role, sponsorship, and other search intents. Internship and
graduate-program classification recognizes common localized and international
terms.

The global and adaptive query budgets now distribute their first selections
across target countries and languages before using remaining capacity on
high-value search intents. Adaptive scheduling retains country coverage under
tight daily limits without discarding learned query repetitions. Existing
provider-attempt limits and outbound thresholds are unchanged.

Candidate profiles default `student_status` to `unknown`. An internship that
explicitly requires current enrollment is held when status is unknown and
excluded when the profile explicitly says the candidate is not a student;
confirmed current students can pass this specific gate. Roles open to recent
graduates are not treated as student-only. The system does not infer current
enrollment or recent-graduate status from education dates.

Focused validation: **51 tests passed** across vocabulary/search planning,
adaptive query learning, decision policy, opportunity classification, and the
golden decision set. The full suite passed **441 tests, 1 skipped**. Ruff,
Python compilation, and `git diff --check` passed. No live discovery, provider
calls, drafts, sends, production workflow, or outbound-limit changes were made
for this Phase 2 pass.

## Phase 2.1 production-readiness check

The full implementation diff is limited to the 11 Phase 2 files listed in
this status entry; no candidate files, CVs, credentials, OAuth tokens, or
private-vault contents are included. The student-status gate only applies to
internships whose text explicitly requires current enrollment. Unknown status
holds that placement, and the profile does not infer enrollment, graduation,
citizenship, residency, or work authorization. Query selection allocates
initial capacity across configured country/language buckets before search
facets and repetitions; canonical configured target roles remain first in the
role vocabulary.

The manual `discovery_only` Actions task was published in commit
`4786f0b7ae74ecd26e0d69b5d47867afe4323986`. CI passed in
[run 38473808222](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38473808222).
The task
restores only the production database snapshot, search profile/preferences,
Tavily cache, and budget ledgers; the task caps search combinations at 12 and
aggregate Tavily API attempts (including retries) at 8. It skips analysis,
action, BTP outreach, follow-ups, Gmail, WhatsApp, broad run artifacts/logs, and
the normal private-vault persistence step. Its artifact is a sanitized summary,
not a copy of candidate files or production database state.

One production run completed as a workflow in
[run 38473922936](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38473922936),
but its sanitized report is `BLOCKED` with reason
`discovery_execution_failed`. It reported zero external results, normalized
listings, duplicates, and new opportunities; zero Tavily attempts, successes,
failures, and retries; and no source or opportunity breakdown. Therefore this
run provides no production opportunity-supply evidence. The underlying
exception was intentionally excluded from the report and the internal log was
removed by the workflow, so diagnose that failure before any further run.

The report confirms no Gmail access, applications, drafts, sent email,
WhatsApp messages, production database writes, or private-vault writes; only
the ephemeral database copy was writable. No Tavily provider attempts were
made. However, successful future queries would update only the ephemeral
runner's restored budget ledgers, while discovery-only is excluded from
private-vault write-back. A subsequent workflow could therefore restore stale
daily counters and spend against the same allowance again. Resolve this
budget-ledger persistence boundary before another live-provider discovery run.

## Phase 2.2 discovery diagnostics and durable Tavily reservations

The first discovery-only artifact and workflow logs recorded zero provider
attempts, zero results, and no source reports, while the CLI's broad exception
handler replaced every exception with `discovery_execution_failed` and the
workflow deleted stderr. The precise production exception is therefore not
recoverable from the retained evidence. A deterministic local failure fixture
reproduces that diagnostic collapse; the CLI now emits a stable phase, reason
code, safe exception class, and controlled message without serializing raw
exception text or tracebacks.

The discovery-only task now keeps its sparse private-vault checkout for the
run and gives each Tavily `DailyBudget` a durable reservation callback. Before
each provider request, the callback fetches current vault `main`, reads the
single fingerprinted ledger, increments its UTC-day count, and pushes a new
normal commit whose tree is checked to differ by exactly that ledger path.
Non-fast-forward races refetch and retry; inability to confirm the commit
blocks provider dispatch. Quota exhaustion is also persisted. Repeated keys
are deduplicated, the shared per-run eight-attempt cap counts retries, and the
general daily limit remains 17 of 20, preserving the three-request BTP reserve.
No candidate, database, CV, OAuth, secret, cache, or application state is
written to the vault by this path.

The workflow now fails when its report is `BLOCKED` even if the CLI wrote a
valid report. Focused deterministic tests cover sanitized diagnostics, missing
and corrupt inputs, durable-before-request ordering, failed reservation,
crash recovery, write scope, stale concurrent pushes, UTC-day boundaries,
exhaustion, duplicate keys, and outbound-path exclusions.

Validation on the final implementation: **53 focused tests passed**;
the full suite passed **466 tests, 1 skipped**. Ruff, Python compilation,
and `git diff --check` passed. The original production exception remains
unidentifiable because its raw diagnostic was deleted before this reporting
schema existed; no specific runtime cause is inferred from the deterministic
failure-path fixture.

The fix was published to `main` as
`c2c1fd40456db3815c296385d85026576003919d`; CI passed in
[run 38514087042](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38514087042).
The committed workflow was re-audited: the discovery-only task's restore and
execution steps were the only selected operational paths; history/BTP,
application, WhatsApp, general artifact/log, and general vault-persistence
steps were skipped. Its sanitized report was the only uploaded run artifact.

One bounded follow-up discovery-only run completed successfully in
[run 38514263381](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38514263381),
but found **0 external results, 0 normalized listings, 0 duplicates, and 0
new opportunities**. All 8 allowed Tavily attempts were live and successful
(0 retries, 0 failed attempts, 0 rate-limit errors); the remaining queries
were budget-deferred. The report records 12 search combinations, 148
source-level deferred queries, and no cache-derived results. It searched
Belgium, France, Canada, Portugal, and Germany across nl/en/fr/pt/de, but
produced no country/source/opportunity-type results, so no representative job
links or eligibility assessment are available. Seven unique key ledgers were
restored; 8 per-attempt reservation commits were durably written before
dispatch, with 94 aggregate daily attempts remaining afterward and the
exhausted key preserved. The run reports 0 Gmail access, applications,
drafts, email sends, WhatsApp messages, and production database writes; its
8 private-vault writes were exclusively the intended Tavily budget ledgers.
The ephemeral database was writable only within the runner. This measures
insufficient discoverable supply for any live-limit increase; retain the
10/day outbound cap.

## Phase 2.3 source-country allocation correction

The zero-result run's source/country pairs exposed two routing defects. The
allocator's `used_query_keys` treated a role/country/language/query combination
as globally consumed after one source used it, even when broad search, ATS
search, and domain-restricted boards represented distinct requests. It also
had no source-country applicability rule, allowing Morocco-only domains to
consume foreign-country requests. Finally, an adaptive country re-rank could
place the first configured target behind learned affinity scores before a tight
combination cap was applied.

Source eligibility is now explicit in `config/sources.yaml`: broad web and
official ATS sources apply to all markets; every Morocco job-board/government
domain is limited to Morocco. Under budget pressure the allocator first gives
an applicable request to distinct broad, official-ATS, and market-specific
scopes, then covers remaining distinct combinations in adaptive plan order.
It retains source priority metadata, country/language fairness, the 12 unique
search-combination cap, and the 8 aggregate Tavily attempt cap. The first
country in candidate target preferences remains the top priority; learned
country affinity continues to order the remaining countries.

Cache identity now includes the source name, domain and ATS filters, query
suffix, result type/limit, and search depth. Reports distinguish plan
combinations from source requests and expose source scope, inapplicable
country combinations, API-attempt and cache-hit country/language pairs, and
provider-returned versus source-filtered counts. A zero-result report
distinguishes live successful-empty responses from cached empties, filtered
results, budget deferrals, failures, and requests that were not scheduled.
Opportunity eligibility remains `NOT_EVALUATED`.

Regression coverage includes Morocco-first planning under a one-combination
cap, Morocco/French/Arabic query coverage under the 12-combination cap,
Morocco-only source routing, international broad/ATS routing, source-scope
cache isolation, filtered-result diagnostics, and the existing durable-budget,
retry, duplicate-key, and BTP-reserve guarantees. Focused tests passed
**110 tests**; the full suite passed **473 tests, 1 skipped**. Ruff, Python
compilation, and `git diff --check` passed. The routing change was published
as commit
[`507da615ba9ee8cad996c5eafa157f036be37164`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/507da615ba9ee8cad996c5eafa157f036be37164);
[CI run 38520602510](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38520602510)
passed.

The first bounded production evaluation,
[run 38520712850](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38520712850),
uploaded its sanitized report but failed with `BLOCKED`,
`discovery_report_serialization_failed` during `report_serialization`
(`DetachedInstanceError`). The diagnostic artifact cannot be used to report
actual opportunity, country/language, source, or provider-result counts: its
fallback contains zero-valued counts, while reporting 8 private-vault ledger
commits. The commits are reservation-boundary writes, not proof of exactly
eight provider requests (quota exhaustion can also write a ledger); no further
provider-attempt count or listing results are available from the sanitized
artifact. Its side-effect record reports no Gmail access, applications,
drafts, email, WhatsApp, or production-database writes; all 8 private-vault
writes were budget-ledger writes. No representative listing URLs or
eligibility conclusions are available.

Code inspection locates the serialization failure in the post-discovery
report path: newly inserted `Job` rows were loaded inside `session_scope`,
then serialized after the session closed; the sanitizer accesses the lazy
`Job.company` relationship. The follow-up loads that relationship eagerly,
with a regression test that detaches the loaded job before sanitizing it. The
follow-up passed the full suite (**474 passed, 1 skipped**), Ruff, Python
compilation, and `git diff --check`. It was published as commit
[`5c372be77e64492dc23fd0a3bfb391424f83bbd9`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/5c372be77e64492dc23fd0a3bfb391424f83bbd9);
[CI run 38521913666](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38521913666)
passed. After explicit authorization, one bounded retry completed successfully
in [run 38522536530](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38522536530)
on commit `95ba94439db6c09e8f81235e7e6cd643927381c5`.

The sanitized retry report recorded 7 distinct search-plan combinations and
8 source requests / Tavily attempts (8 successes, 0 failures, 0 retries, 0
rate-limit errors); 38 source requests were deferred by budget. There were no
cache hits. Providers returned 80 results; source filtering excluded 36,
leaving 44 external results, all normalized, of which 19 were duplicates and
25 were new records (all from live results). Those 25 are **normalized
discovery records, not 25 verified vacancies**: report titles include salary
pages, occupational-reference pages, and discussion threads. The report's
type classifier counted 21 JOB, 2 INTERNSHIP_STAGE, 1 GRADUATE_TRAINEE, and 1
UNKNOWN; it did not separately identify a PFE placement. Eligibility for all
records remains `NOT_EVALUATED`.

The plan searched Morocco 3 times, Belgium 2, France 2, and Canada 1; language
coverage was Arabic 2, Dutch 1, French 3, and English 2. The report does not
retain a joint country/language matrix. The 8 live attempts were broad web
search 4 (countries: Morocco, Belgium, France, Canada; languages: Arabic 1,
Dutch 1, French 2), official-ATS search 3 (countries: Morocco, Belgium,
France; languages: French 1, English 2), and Morocco-only ANAPEC 1 (Morocco,
Arabic). All 25 new normalized records came from broad web search: Morocco
11, Belgium 1, France 7, Canada 6; result-language counts were Arabic 11,
Dutch 1, French 13. Morocco-only source applicability excluded 110
foreign-country source/combination pairs. Broad web search returned 48
provider results (4 filtered); official-ATS search returned 24 (all 24
filtered); ANAPEC returned 8 (all 8 filtered). No direct official vacancy or
verified application route was established. For example, the
[VINCI careers search page](https://jobs.vinci.com/en/location/morocco-jobs/1440/2542007/2)
is an employer careers portal, but is not a specific verified vacancy; the
[Canada Job Bank result](https://www.jobbank.gc.ca/marketreport/summary-occupation/17969/ca)
is occupational information, not a job listing. Other representative URLs
were third-party search/index pages and should not be treated as direct
employer vacancies.

The report labels all 25 records `DATED_WITHIN_WINDOW`, but this is **not
source-verified freshness**: the Tavily connector supplies no posting date and
`app/normalization.py` currently substitutes the collection time when a date
is missing. Actual vacancy dates are therefore unknown. This and the noisy
result classification are remaining discovery-quality limitations.

Seven unique budget ledgers were restored; after the 8 durable reservation
commits, the report showed 78 aggregate daily attempts remaining
(per-key remaining: 0, 0, 10, 17, 17, 17, 17). The BTP reserve and daily
limits were preserved. The successful run reports 0 Gmail access, application
attempts, drafts, sent email, WhatsApp messages, and production-database
writes; the 8 private-vault writes were exclusively the intended Tavily
budget-ledger commits, and runtime database writes were ephemeral. The 12
combination / 8 attempt caps remained enforced. No further production run was
dispatched; the 10/day outbound limit remains unchanged.

Gmail mailbox reconciliation remains `NOT_CHECKED`. The supported OAuth flow
is `wca gmail-auth`; in live mode it requests the existing `gmail.send` and
`gmail.compose` scopes plus optional `gmail.readonly`, and uses the configured
client secret with a browser consent flow on `http://localhost:18320/`.
Authorization saves to the configured git-ignored token path, replacing that
token file. Obtain interactive owner consent and securely preserve the current
token before invoking it; no credentials were read, printed, reauthorized, or
overwritten in this pass. After consent, verify scope presence with
`wca gmail-status`. Current read-only recovery code uses that scope to reconcile
specific pending/uncertain sends and drafts; it does not perform a full
mailbox-wide audit against all WCA applications and drafts. That broader
comparison still requires a bounded read-only reconciliation task before any
duplicate-clearance claim or retry.

No scale-up is supported by measured production discovery evidence. Keep the
current configured daily outbound cap unchanged; a 20/day proposal is
unsupported until verified fresh eligible supply, automatic-email route
proportion, duplicate/cooldown suppression, route-verification, eligibility,
and provider/recovery-failure rates have been measured.

## Read-only employer-history preflight

The manual `history_preflight` task restores only `state/agent.db`,
`state/btp_outreach_state.json`, `candidate/profile.yaml`, and
`candidate/preferences.yaml` from the private vault using sparse checkout. It
does not restore OAuth/API-key files, build the normal API-key `.env`, execute
discovery or outreach, or run provider calls. Its CLI uses an immutable
SQLite `mode=ro` connection, refuses non-empty SQLite WAL/journal sidecars,
checks the restored database hash before and after reading, and never calls
database initialization or migration. Missing, corrupt, inconsistent, or
incompletely restored state returns `UNKNOWN`; no matching WCA row is reported
as a scoped database result, not proof of mailbox-wide clearance.

The sanitized report separately counts application outcomes, WCA-tracked
drafts, unresolved/active live-send and draft attempts, employer cooldowns,
BTP suppression records, and daily outbound limits for ODBM, TGCC, El Ouatik
Frères, CETEMCO, and MEGEC; history checks can be limited to an allowlisted
employer. Gmail
mailbox history is always `NOT_CHECKED`; submission clearance remains unknown
when that separate history cannot be verified. The report contains no message
content, contact addresses, draft IDs, or candidate identity values. Route
evidence is marked previously reported but not re-crawled by the read-only
history task. The manual BTP task accepts only three allowlisted employer IDs,
starts from each fixed official careers URL, skips broad discovery for targeted
runs, and filters the candidate set to that employer before applying existing
identity, duplicate, cooldown, route-verification, claims, daily-limit, and
delivery gates. A target is a selector, not authorization. Targeted
`email_mode=dry_run` restores only the production DB, BTP state, candidate
profile/preferences, and CV; it disables LLM/WhatsApp credentials, rolls back
all database work, refuses to initialize a missing DB, does not persist
screening state, and skips WhatsApp and vault-persistence steps. It validates
current route evidence and the generated application without a Gmail call or
draft. An explicitly labeled `Postuler:
email` route can qualify a general contact address; other general contacts
remain excluded unless the existing general-contact option and recruitment
evidence permit them. CETEMCO's current listing requires an exact vacancy title
in the subject and verified CV fit, which the generic spontaneous sender cannot
provide, so that targeted email route is blocked. ODBM and TGCC remain report-only portals because WCA
does not submit web forms. The history task independently skips normal vault
restoration, `.env` creation, outreach, WhatsApp hooks, and state persistence,
including on failure; it uploads only the sanitized report.

Targeted official-site crawls for MEGEC and El Ouatik initially failed because
both careers URLs redirect to their trailing-slash forms, while the crawler
rejected all redirects. Website research now has an opt-in, bounded redirect
mode that follows at most four HTTPS redirects on the same employer domain
(including its `www` variant), rejecting cross-domain, HTTP, credentialed, and
nonstandard-port destinations. It is enabled only for allowlisted targeted
preflight and BTP research; general crawling retains its existing redirect
behavior.

The published read-only history run **38381116469** completed on commit
`08c9e6430aadcc4f15755f7160db0933a517a975`; it read the restored production
database without modification and found no WCA-tracked matching records for
the selected employers. Gmail mailbox history was `NOT_CHECKED`, so this was
not full mailbox-wide duplicate clearance. Published targeted MEGEC and El
Ouatik runs **38381320878** and **38381614655** failed closed on the redirect
handling limitation and created no drafts or sends.

The redirect correction was published as commit
`00ab25cca0052216ba5451fa8b6ab7dbc5d13928`. MEGEC's targeted non-sending run
**38383879263** completed successfully and followed only its bounded
same-domain HTTPS canonical redirect. The official page,
“Candidature spontanée — MEGEC”
(`https://www.megec.com/candidature-spontanee/`), invites beginner and
experienced technicians in civil engineering and related construction
specialties and explicitly says “Postuler : Contact@megec.com”. WCA verified
this as an `level_a_explicit_email_instruction` route, distinct from a generic
contact address.

The same run reloaded production WCA state: no matching company/alias,
application, WCA-tracked draft, unresolved live-send or draft attempt,
cooldown, or BTP suppression row was found. The earlier immutable history
report provides the corresponding reason codes and reports unchanged DB hash.
Gmail mailbox history remains `NOT_CHECKED`; do not interpret this as
mailbox-wide duplicate clearance. Investigation confirmed that the exact
568,646-byte French PDF already exists at the private-vault
`candidate/cv/CV_Omar_Benhamid_FR.pdf` path and matches the local source bytes.
The previous `no cv` result came from the targeted restore step copying the
vault CV directory into a pre-existing `candidate/cv` directory, nesting the
file one level too deep. The targeted restore now copies the directory
contents into the configured location and has a regression assertion. The
owner requested Morocco be prioritized; the private preferences were updated
to list Morocco first while retaining `sponsorship_required: true`. MEGEC's
published page does not establish sponsorship availability, so a Moroccan
application must remain blocked unless that requirement is satisfied without
relaxing it. The restore fix was published in `a6bf933`; targeted MEGEC
preflight **38386255517** then returned `dry_run_validated`, re-fetched the
official route, resolved the French CV, and passed the existing safety gate
with **0 sends and 0 drafts**. A fresh production history preflight,
**38386390488**, again found zero WCA-tracked application, draft, active or
uncertain attempt, cooldown, or suppression records; the database hash was
unchanged and no provider, messaging, or vault-write side effect occurred.
Gmail mailbox history remains `NOT_CHECKED`, and the history report retains
`submission_clearance: UNKNOWN`. The saved sponsorship requirement remains
unsatisfied by available official evidence; therefore no live run was
dispatched and no email or draft has been created.

Latest focused redirect/BTP/history run: **116 passed**; full suite:
**423 passed, 1 skipped**. Ruff, Python compilation, and `git diff --check`
passed. Tests use deterministic fixtures; these results do not establish
candidate-fit clearance or Gmail mailbox history.

## Domestic sponsorship policy and MEGEC application

The saved `sponsorship_required: true` preference now applies to overseas jobs,
not Moroccan domestic jobs. The decision gate holds an overseas opportunity
until explicit sponsorship evidence is present in its structured signal or
posting text; explicit unavailable-sponsorship wording overrides a positive
signal. Existing score, freshness, experience, and other decision rules retain
precedence. The read-only history report no longer calls a Morocco target plus
an international sponsorship requirement a preference conflict. It separately
reports that this profile schema has no citizenship/residency field; no such
status is inferred or included as a claim in outbound text.

The Morocco-first preference was preserved in the private vault with
`sponsorship_required` unchanged. MEGEC's official route was re-fetched during
targeted preflight **38389168933** and still showed “Postuler :
Contact@megec.com” at
`https://www.megec.com/candidature-spontanee`. The French CV resolved at its
configured private-vault path. The dry-run safety gate passed with zero sends
and zero drafts. Fresh read-only production check **38389065571** showed no
matching WCA application, draft, attempt, cooldown, or suppression at
preflight time, with the database hash unchanged. Gmail mailbox history
remains `NOT_CHECKED`; it is not full mailbox-wide duplicate clearance.

The authorized live MEGEC run **38389327414** completed successfully on
`8f3b0d469ae4cdcb0dc425055c8ebc107177a4d8`: WCA reports one send, Gmail
returned a message ID, and delivery stage is `gmail_send_confirmed`. The
existing database/vault persistence step completed. Post-send read-only
verification **38389583316** found one application with outcome `sent` and
one live-send record in `confirmed_sent`; the active lock, cooldown, and
suppression now correctly block any duplicate. No retry was performed. Gmail
mailbox history outside the WCA-tracked state remains `NOT_CHECKED`. The live
run did not claim Moroccan citizenship/residency or sponsorship in the message.

Policy validation: focused sponsorship/preflight/golden tests **31 passed**;
full suite **429 passed, 1 skipped**; Ruff, Python compilation, and
`git diff --check` passed.

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

## Phase 2.4 vacancy verification and posting-date evidence

Discovery records now distinguish an individual vacancy from a research page
and separately track whether a vacancy is verified. Search results and
unstructured job-board links remain unverified; a vacancy is verified only
when its structured record has a stable source ID, employer, title, location,
and an official ATS URL. The application backstop rechecks the persisted
verification state, reason code, ATS source type, and official ATS host before
allowing an application action. Analysis and pending-action selection also
hold unverified vacancies; spontaneous BTP handling remains separate.

Unknown posting dates remain unknown instead of being replaced with
collection time. Connectors retain only explicit publication/posting dates
with source-field and evidence-URL provenance; record creation and update
timestamps are not treated as posting dates. Reports distinguish verified
vacancies, unverified vacancy candidates, research records, and rejected
non-vacancies, and expose date/entity evidence with sanitized public URLs.
Legacy database rows receive conservative unknown/research-required
classification defaults without rewriting their existing dates. Expired
closing dates block application.

The existing discovery-only isolation, Morocco-first allocation, durable
per-attempt Tavily reservations, 12-combination and 8-attempt caps, daily
budgets, BTP reserve, and 10/day outbound limit are unchanged. No live
provider calls, candidate/application writes, outbound actions, or MEGEC
reprocessing have occurred for this implementation.

Validation: full suite **483 passed, 1 skipped**; Ruff passed on all changed
Python files; Python compilation and `git diff --check` passed. Repository-wide
Ruff still reports 25 findings in untouched files. Publication, CI, and the
authorized single bounded discovery-only evaluation are pending; no production
discovery measurements are claimed yet.

### Phase 2.4 discovery execution recovery

The authorized bounded discovery-only evaluation
[38529148328](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38529148328)
was blocked with `OperationalError` during `discovery_execution`, before it
produced a usable discovery report. Its sanitized artifact reports four
private-vault writes, all budget-ledger writes; that count does not establish
how many provider attempts were dispatched. No email, draft, WhatsApp,
application, or candidate/database write-back path ran. The workflow removed
its internal error log, so the exact production SQLite message is unavailable.

A stale restored-database schema is the likely cause, not a confirmed remote
exception: a local regression reproduces the ORM `OperationalError` when
reading the prior schema with the new Phase 2.4 model, then confirms the
additive migration restores the read. Discovery-only now checks every ORM
table and the restored database's integrity before calling the existing
idempotent schema initializer on the ephemeral runner copy. Provider reporting
also distinguishes confirmed durable reservations from other ledger writes.
The full suite passes **484 tests, 1 skipped**; focused tests, Ruff on changed
files, compilation, and `git diff --check` pass. The fix remains unpublished;
no follow-up production evaluation has been dispatched.
