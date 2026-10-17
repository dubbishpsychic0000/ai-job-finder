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
files, compilation, and `git diff --check` pass. The fix is published, and the
follow-up evaluation has completed after CI passed.

Recovery commit
[`a00b54b0f94a184d83c1f5d6d0dc9cb7c9fa92d8`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/a00b54b0f94a184d83c1f5d6d0dc9cb7c9fa92d8)
is on `main`; [CI run 38531463259](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38531463259)
passed. The one bounded follow-up
[discovery-only run 38531637992](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38531637992)
completed successfully and uploaded only the sanitized report. Its status is
`COMPLETED`, with 7 search-plan combinations and 8 source requests / Tavily
attempts (8 successful, 0 failed, 0 retries, 0 cache hits); 38 queries were
deferred by the daily provider budget. The source split was 4 broad web-search,
3 official-ATS search, and 1 ANAPEC Morocco request. All 8 reservations were
durably recorded before dispatch. The report shows 7 restored ledgers, 49
aggregate daily attempts remaining afterward, and no changed daily query
limit.

Providers returned 80 results; 28 were filtered and 52 normalized, including
29 duplicates and 23 new discovery records. Those records comprise 5
unverified vacancy candidates, 16 research-required records, and 2 rejected
non-vacancies; there were **0 verified individual vacancies** and therefore
**0 genuinely new verified opportunities**. All 23 new records lack
verifiable posting-date evidence, and eligibility remains `NOT_EVALUATED`.
Reported query coverage was Morocco 3 (Arabic 2, French 1), Belgium 2 (Dutch
1, French 1), France 2 (French 1, English 1), and Canada 1 (English 1).
Opportunity-type labels were 18 jobs, 1 internship/stage, 3 graduate/trainee,
and 1 unknown. New-record country labels were Morocco 3, Belgium 7, France
7, and Canada 6.

The report detected 7 unverified application-route indications (5 company
portals and 2 phone indications); none was verified. Representative direct
ATS URLs include [City of Fort Worth Engineering Technician II on
Greenhouse](https://boards.greenhouse.io/cityoffortworth/jobs/8015004003)
and [Junior Civil Engineer on
SmartRecruiters](https://jobs.smartrecruiters.com/JobsForHumanity/744000064236969-junior-civil-engineer).
These are discovery evidence only: the report marks them unverified, gives no
posting date, and does not establish that they remain open or that the
candidate is eligible.

The run reports 8 private-vault writes, all restricted to Tavily budget
ledgers; production-database writes, Gmail access, applications, drafts, sent
email, and WhatsApp messages were all zero. Ephemeral database writes were
reported as enabled for local discovery state. Outbound limits remain
unchanged at 10/day; the confirmed MEGEC application was not retried.

## Phase 2.5 evidence-backed vacancy verification

The sanitized report for discovery-only run
[38531637992](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38531637992)
shows 5 `VACANCY_UNVERIFIED` candidates, 16 `RESEARCH_REQUIRED` records, and
2 rejected non-vacancies; it establishes no verified vacancies. Code-path
inspection confirms the primary missing evidence: Tavily results retained
search snippets and no detail-page request was made, so the 23 new records
had no source-backed posting dates. The old classifier could trust some
structured-looking ATS records without checking current posting status, and
URL-derived employer slugs were not independent employer evidence. It also
classified a salary/reference result as a possible vacancy. These facts
explain the absence of verification; they do not establish that the five
unverified leads are genuine or still open. The deleted internal log leaves
no further production exception hypothesis to confirm.

The local implementation adds conservative public detail verification for
exact Greenhouse and SmartRecruiters posting URLs. It checks the stable posting
ID, employer, title, location, meaningful description, the currently
published ATS record, and an ATS-hosted application route. HTTP 200, URL
shape, search wording, collection time, or a query term do not prove an open
job, posting date, internship, PFE, or candidate fit. Explicit closed status
or elapsed source-reported closing date is separate from posting-date
confidence. Unknown, unsupported, inaccessible, ambiguous, and expired
records remain blocked with evidence timestamps, URLs, and stable reason
codes. Generic category/careers/reference/discussion results remain leads or
are rejected as non-vacancies. Detail checks are capped at 12 per run. Only
Greenhouse and SmartRecruiters have source-specific verification adapters so
far; every other ATS and unsupported page type remains explicitly unverified
until a similarly auditable adapter or manual research is available.

Previously stored ATS leads are rechecked when rediscovered, so a
`verification_required` record can return to normal analysis only after fresh
evidence confirms it. Candidate eligibility analysis now stops before
matching/scoring when the vacancy proof tuple is absent. Application selection
and the send-time gate both require a recent verified individual vacancy;
send-time evidence must be fresh, match the stored title/employer/location/
description, and confirm the same ATS application route. Unverified pages do
not contribute confirmed opportunity types or qualification evidence to the
sanitized report.

The sanitized discovery-only report uses schema version 3 and separates
detail checks, reused detail evidence, open/closed/unknown/unverified outcomes,
source-level reason codes, and existing leads reverified from genuinely new
records. Its existing 12-combination/8-provider-attempt caps, durable Tavily
reservation boundary, UTC-day budget accounting, BTP reserve, and 10/day
outbound limit are unchanged. The full suite passed **502 tests, 1 skipped**;
Ruff passed on changed Python files; Python compilation and `git diff --check`
passed. Repository-wide Ruff still reports 25 findings in untouched files.
Before publication, the implementation had not dispatched discovery, written
production candidate/application state, created drafts, sent email or
WhatsApp, or reprocessed the confirmed MEGEC application. The subsequent
publication, CI result, and single bounded follow-up evaluation are recorded
below.

### Phase 2.5 bounded evaluation

After commit
[`776b321b699a78c40b57f706d40adb4e4b9d2780`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/776b321b699a78c40b57f706d40adb4e4b9d2780)
passed [CI run 38542559107](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38542559107),
one bounded discovery-only run completed in
[run 38542750328](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38542750328).
The sanitized artifact reports 80 provider results, 34 source-filtered results,
46 normalized results, 26 duplicates, and 20 new discovery records. Those 20
are not verified vacancies: the report has **0 verified open vacancies**, 1
unverified individual-vacancy candidate (`unsupported_detail_verification_source`),
16 research-required leads, and 3 rejected non-vacancies. All 20 new records
have unknown posting dates. No candidate eligibility was evaluated.

The seven selected search combinations covered Morocco (3), Belgium (2),
France (2), and Canada (1), using Arabic (2), Dutch (1), French (3), and
English (2). All eight live provider attempts succeeded without retries:
four broad web searches returned 48 provider results (2 filtered, 46
normalized); three official-ATS searches returned 24, all source-filtered; and
one Morocco/Arabic ANAPEC request returned 8, all source-filtered. The broad
search attempts covered Morocco, Belgium, France, and Canada; official ATS
covered Morocco, Belgium, and France. No Morocco-specific board request was
attempted because the remaining requests were budget-deferred. The one
individual-vacancy candidate points to an unsupported ATS/detail source and
remains unverified; a representative research lead was Bayt's Morocco
engineering-technician category page, not an individual vacancy.

Seven durable key ledgers report **41 aggregate daily attempts remaining**
(per-key remaining: 0, 0, 0, 0, 7, 17, 17). The artifact confirms all 8
provider reservations were durably committed before dispatch and counted
against those daily ledgers; the 12-combination, 8-attempt, daily per-key,
BTP-reserve, and 10/day outbound limits remain unchanged. The run reports zero
Gmail access, applications, drafts, email, WhatsApp, and production database
writes; its 8 private-vault writes were exclusively budget-ledger reservations.
No application, candidate/application state, or MEGEC retry occurred.

## Phase 2.6 source filtering and allocation diagnostics

The Phase 2.5 artifact proves the outcome counts, but it does **not** preserve
the rejected provider URLs or filter reason codes. Therefore, the specific
reason all 24 official-ATS and 8 ANAPEC results were filtered cannot be
reconstructed from that run. The source implementation did apply an official
ATS restriction and ANAPEC domain scope, but attributing those historical
rejections to either rule would be speculation. The one new individual-vacancy
candidate was on Expertini, which has no supported detail verifier here; one
unsupported example is not sufficient evidence to add a source adapter.

The Morocco-specific requests were deferred by the **8-attempt run cap**, not
by exhausted daily key ledgers: the sanitized artifact recorded 41 aggregate
daily attempts remaining after eight durable reservations. Code inspection
showed allocation gave first coverage to broad search, official ATS and one
market-specific scope, then prioritized uncovered plan combinations before
additional Morocco-only source scopes. This explains the recorded four broad,
three ATS and one ANAPEC attempts without incorrectly treating remaining
per-key capacity as an unreserved allowance.

Phase 2.6 now prioritizes up to two additional distinct Morocco-only source
scopes within the existing run allowance before repeating broad/ATS searches,
while preserving applicability, source-scoped cache identity, distinct plan
coverage, durable per-key reservations and the shared eight-attempt cap. It
does not increase provider volume or relax vacancy verification. Diagnostics
now retain filter reason counts and sanitized host/path-pattern examples,
normalization rejection reasons, provider/accepted/filtered result counts by
country and language, per-key reservation outcomes by short fingerprint,
source-applicability exclusions, and run-cap versus daily-budget deferrals.
The official-ATS filter now validates the parsed URL hostname rather than
trusting ATS text embedded elsewhere in a URL; ANAPEC scope also validates the
parsed hostname. Excess results beyond the requested provider-result limit are
counted as filtered rather than silently disappearing.

The old run cannot establish which URL patterns were rejected, so no claim of
improved vacancy yield is made yet. A later sanitized report will distinguish
verified vacancies, unverified candidates, research leads, rejected
non-vacancies, duplicates and unsupported verification sources; it will also
report source and query coverage. Unsupported pages remain unverified, and
posting-date confidence remains independent from current vacancy status.
The implementation performs no eligibility assessment, production
candidate/application write, draft, email, WhatsApp message, or MEGEC retry.

Validation: full suite **507 passed, 1 skipped**; focused discovery/filtering
suite **61 passed**; Ruff on all changed Python files, Python compilation, and
`git diff --check` passed. The verification timestamp regression assertion now
compares the same UTC instant regardless of whether SQLite returns an aware or
naive datetime. Publication, CI confirmation, and any bounded discovery-only
evaluation were pending at implementation validation.

After publication, CI passed in
[run 38549577407](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38549577407).
One bounded discovery-only evaluation then completed successfully in
[run 38549804215](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38549804215).
The workflow ran only the bounded discovery step; application, employer preflight, BTP, WhatsApp, and general pipeline steps
were skipped. The 7,141-byte sanitized artifact was created, but repeated
archive downloads failed with GitHub artifact-storage DNS/connection errors,
so its result counts and post-run durable budget have not yet been parsed and
are intentionally not reported here. Before dispatch, the latest inspected
artifact showed 41 aggregate daily attempts remaining and no intervening
provider run. No second evaluation was dispatched.

## Phase 2.7 Emploi-Public public competitions

The government connector previously covered RSS/JSON feeds only; Emploi-Public
was merely a Tavily search scope, with no official detail-page parser or
competition-specific verification. Consequently, indexed links could not
establish a current public competition through deterministic source evidence.
Inspection of a French detail page also found conflicting submission
instructions (an online administration portal and an email deposit address).
That conflict is direct evidence of an unresolved route, not proof that either
route is valid; the inspected page remains unverified and no application was
attempted.

Added a Morocco-only Emploi-Public adapter that checks the portal's robots
policy, restricts requests to canonical HTTPS competition-detail UUID paths,
caps listing/detail requests, parses French and Arabic competition evidence,
and does not fetch robots-disallowed document links. Verification requires an
explicit active announcement stage, identifiable role and administration,
location, substantive description, future deadline, and consistent explicit
submission route. Posting-date evidence is stored independently from status;
missing or malformed posting dates do not make an otherwise supported
announcement closed. Inaccessible, redirected, ambiguous, expired, and
procedural pages receive explicit safe outcomes. Procedural notices can update
matching records by portal UUID but do not create candidate jobs.

The existing candidate fields and enums are reused, with one idempotent
`public_competition` JSON-column migration for source-specific stage,
requirements, evidence and owner-review data that scalar fields cannot
represent. Discovery reports sanitize the persisted evidence and include
source, country, language, request, verification and procedural-notice
outcomes. Only a verified open competition may enter the deterministic
eligibility checklist, which remains `HOLD`: candidate eligibility is not
assessed, owner approval is required but not granted, and the normal action
workflow plus `ApplicationEngine` both block submission. No automated
application, draft, email, WhatsApp message, production candidate write, or
MEGEC retry is part of this integration.

Validation: the complete test suite passed (**521 passed, 1 skipped**),
including offline French/Arabic fixtures, robots/redirect behavior, evidence
and date handling, procedural notices, persistence, legacy-schema migration,
and downstream submission blocks. After the final defensive sanitizer and
idempotent-record-update edits, Ruff, Python compilation, and
`git diff --check` passed. No bounded evaluation was run: the previous discovery
artifact could not be retrieved, so the post-run durable provider balance is
unknown. Direct portal retrieval is separately bounded and does not consume or
borrow Tavily reservations. Remaining limits include the portal's changing
HTML, uncertain Arabic field variants, and contradictory submission routes;
unknown or inconsistent evidence fails closed.

## Phase 2.8 bounded live validation of Emploi-Public discovery

The repository was rechecked on `main` at
[`8c3937eeeb27c13c17a7c364c5b3e975b5c382e3`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/8c3937eeeb27c13c17a7c364c5b3e975b5c382e3),
with CI passing in [run 38565428201](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38565428201).

A fresh bounded discovery preflight was attempted with the repository's guarded
`discovery-only` command:
`python -m app.cli discovery-only --report data/discovery_only_report.json`.
The command did not dispatch any provider request. It failed in the
`input_restore` phase because the restored discovery inputs were not present in
`data/`, and the private-vault budget store was not configured or readable.
Evidence:

- `TAVILY_BUDGET_VAULT_REPO` is empty in the current environment.
- `data/discovery_only_inputs_status.json` is absent.
- The expected secure vault repository at `../ai-job-finder-vault` contains no
  `state/tavily_budget_*.json` ledger files.
- The generated sanitized report is `data/discovery_only_report.json` and reports
  `reason_code: "production_inputs_invalid_or_incomplete"` with status
  `BLOCKED`.

This satisfies the hold condition in the repository's safety rules: no
Tavily/API call, direct public ATS request, budget reservation, application,
email, WhatsApp, BTP action, production candidate write, or MEGEC retry was
performed. The discovery-only path remains isolated and no further evaluation
was dispatched because the ledger and restored inputs cannot be reconciled.

Measured result from the bounded preflight run:

- Status: `BLOCKED`
- Reason code: `production_inputs_invalid_or_incomplete`
- Phase: `input_restore`
- External results fetched: 0
- Normalized results: 0
- Verified open competitions: 0
- Unverified candidates: 0
- Research-required records: 0
- Provider API attempts: 0
- Remaining run attempts: 8

The live source validation is therefore intentionally withheld pending a valid
restored vault, current production snapshot, and reconciled daily budget ledger.
No discovery changes were made during this validation step.

## Phase 2.8a discovery input restoration hardening

### Confirmed cause

The Phase 2.8 preflight was invoked directly in the local checkout rather than
as the `discovery_only` GitHub Actions task. The restore manifest and
`TAVILY_BUDGET_VAULT_REPO` are runtime inputs created/configured only by
`.github/workflows/agent.yml`; they are not expected to exist in a normal local
checkout. The local command therefore stopped at `input_restore`, before a
provider request. The local `../ai-job-finder-vault` checkout was also not a
current authority: its checked-out branch was ahead of and 145 commits behind
its cached `origin/main`, and it had no Tavily ledger files. Its cached
`origin/main` tree did contain Tavily ledger paths, but that tracking ref was
from 2026-10-13 and was not used to infer any current balance.

Recent GitHub workflow metadata corroborates that the intended execution path
is Actions-managed: bounded run
[38549804215](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38549804215)
shows both “Restore bounded discovery inputs only” and “Run bounded
discovery-only evaluation” completed. Its report artifact could not be
recovered in Phase 2.8, so this history does not establish current ledger
balances or successful source yield.

The restore step had a separate proven integrity/reporting flaw: it set
`provider_budget_ledgers_restored` from whether key-derived ledger path names
were generated, rather than whether every expected ledger existed and was
valid. It also cloned the vault's default branch without explicitly pinning
`main`. The CLI did fail closed when a manifest was absent or `ready` was
false, but it accepted an underspecified `{"ready": true}` manifest and did
not tie the manifest to the checkout path used by durable reservations.

### Repair

Added `app/workflows/discovery_only_restore.py` as the single restoration
validator/copy path. The workflow now explicitly clones only `main`, sparse
checks out required inputs and key-derived ledger paths, then calls this
validator. Before any input is copied or the manifest is marked ready, it
checks the configured vault origin, checked-out branch and equality with
`origin/main`; requires the database, profile, preferences and non-empty
provider keys; and validates every per-key ledger's JSON shape, UTC date,
exhaustion flag and count against Tavily's unchanged 20-request hard limit.
It preserves ledger content and never derives a balance from reports. Missing
or corrupt ledgers, an invalid origin/ref, or incomplete configuration writes
a sanitized `ready: false` manifest with a stable reason code.

The discovery-only CLI now requires the complete versioned restore manifest,
checks that key and ledger counts match the restored keys, revalidates local
ledger contents, and requires the durable reservation repository path to be
the same checkout attested by the manifest. A missing/invalid manifest,
missing vault configuration, absent/corrupt ledger, wrong ref, or path
mismatch blocks before discovery dispatch. The 8-attempt run cap, 17-per-key
general daily allowance, three-request BTP reserve, and outbound protections
are unchanged.

### Validation and limits

Offline tests cover absent/invalid manifests, missing and corrupt ledgers,
future/out-of-range ledger state, missing vault configuration, wrong origin,
wrong branch, vault-path mismatch, and successful restoration from a local
Git fixture while preserving its ledger balance. CLI tests assert blocked
restore states never invoke discovery. No live discovery, provider request,
production write, or outbound action was run in this phase. The actual current
durable ledger balance remains unverified; restoration must run in the
configured GitHub Actions environment before any separately authorized
bounded live validation is considered.

Validation completed: full suite **536 passed, 1 skipped**; Ruff on changed
Python files, Python compilation, and `git diff --check` passed. Publication
of the implementation is commit
[`e907b3eb50bdecbd6651dc2f932091e7f4405133`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/e907b3eb50bdecbd6651dc2f932091e7f4405133);
its CI passed in
[run 38570895688](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38570895688).

## Phase 2.8b restore-only GitHub Actions preflight

Added a dedicated `discovery_restore_preflight` manual workflow task and job.
It runs the same hardened private-vault restore validator against the
configured vault `main` checkout, but is isolated from the application
pipeline: the normal pipeline job is skipped, and the restore job has no
discovery, Tavily connector, reservation store, application, email, WhatsApp,
or BTP command. The only uploaded artifact is a sanitized restore report.

The report includes expected/restored required-input and ledger counts,
validated origin/branch/reservation-checkout booleans, UTC-day ledger status,
aggregate and per-key remaining capacity using only short SHA-256 key
fingerprints, and the state of the 3-request BTP reserve. Capacity is reported
only for current-UTC-day ledgers; stale, malformed, exhausted, or reserve-
consuming state yields a blocked report. The restore flow never edits vault
ledger contents or reconstructs balances from past artifacts.

Offline validation: full suite **541 passed, 1 skipped**; after the final
reservation-checkout assertion, focused restore-only suite **40 passed**.
Ruff, Python compilation, and `git diff --check` passed.

The restore-only task was dispatched once after CI passed for commit
[`158d2b042d8af0b0b9c62c1ff4d0783a3c4ba42c`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/158d2b042d8af0b0b9c62c1ff4d0783a3c4ba42c).
The exact run is [38576950879](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38576950879).
The sanitized `discovery-restore-preflight-38576950879` artifact uploaded
successfully. The restore-only job failed closed with
`provider_budget_exhausted`; the normal `pipeline` job was skipped.

Measured artifact results (UTC day `2026-10-14`):

- Vault origin, `main` branch, and exact reservation checkout path: verified.
- Required inputs: **4 expected, 4 restored**.
- Provider keys and current-day durable ledgers: **7 expected, 7 restored**;
  all ledgers passed format/date/range validation and no content was modified.
- Per-key daily request counts: two keys at 20/20; four at 17/20; one at 1/20.
  The artifact reports only short key fingerprints, not credentials.
- General (non-BTP) remaining capacity: **16 aggregate**, available on the
  key at count 1; no general capacity remains on keys at 17 or 20.
- BTP reserve remaining: **15 aggregate**, with three reserved requests still
  available on each of five keys. Two keys at the total 20-request cap have no
  remaining BTP capacity, so the report marks BTP reserve validation blocked
  and the overall preflight does not authorize discovery.
- Requests dispatched: **0**. Reservations created: **0**. Applications,
  drafts, emails, WhatsApp, BTP actions, and production candidate writes: **0**.

The one restore-only run was not repeated. No live discovery was chained from
the partial remaining aggregate capacity; the failed closed artifact is the
current evidence and does not authorize a provider run. Further provider use
requires a separate fresh budget preflight and explicit authorization.

## Phase 2.8c Tavily general-capacity gate and UTC rollover

The restore-only artifact demonstrated that the previous all-keys gate was
overly restrictive: discovery had 16 general-purpose attempts remaining on one
key, while the other keys had reached the 17-request general limit or the
20-request total limit. The gate now authorizes only when at least one
validated key has general capacity. Keys without general capacity are skipped;
they do not consume another key's budget. A durable reservation independently
clamps every discovery reservation to the 17-request general limit, preserving
the three-request per-key BTP reserve.

Current-UTC-day ledgers are reported with their actual remaining capacity.
Prior-day ledgers are reported as rollover-eligible pending a successful
durable reservation; no request is dispatched until that reservation succeeds.
Restore-only reporting does not mutate or reconstruct ledger contents.
Future-dated or malformed ledgers still block the gate. The reservation layer
alone resets a stale-day ledger, atomically recording today's first request
before provider dispatch.

Added offline coverage for mixed 20/17/1 key states, general-cap reservation,
and non-mutating prior-day reporting. A mocked-provider regression checks that
only the 1/20 key can dispatch in that mixed state. Full suite: **548 passed,
1 skipped**. Ruff, Python compilation, and `git diff --check` passed. A single
restore-only Actions preflight was run after publication and CI; it did not
dispatch discovery.

Publication and post-publication check:

- Implementation commit [`ec3617d20b1f3a99548fd21e97bd96d4727c9b6b`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/ec3617d20b1f3a99548fd21e97bd96d4727c9b6b)
  was pushed to `main`; CI passed in [run 38653751046](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38653751046).
- One restore-only task ran as [run 38653889188](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38653889188).
  Its sanitized report artifact uploaded, and the normal pipeline job was
  skipped.
- On UTC day `2026-10-14`, the actual configured vault origin, `main` ref, and
  reservation checkout path were verified. Inputs restored: **4/4**; valid
  durable ledgers: **7/7**. All ledgers matched the current UTC day.
- The seven keys were at counts **20, 20, 20, 18, 17, 17, 17**. Aggregate
  capacity remaining under the 20-request total cap was **11**; general
  capacity was **0**. The computed remaining BTP capacity was **11**, but
  3-request BTP-reserve validation was blocked because some keys had already
  consumed reserve capacity. No key was eligible for general discovery, so the
  result was `BLOCKED` with `no_general_budget_available`.
- This run made **0** Tavily requests and **0** reservations. Application or
  outbound actions, BTP actions, production candidate writes, and the normal
  pipeline were all **0**. The preflight did not authorize discovery, and no
  live discovery was dispatched.

## Phase 2.8d post-rollover restore-only validation

After UTC rollover, exactly one restore-only preflight ran on `main`:
[run 38657433652](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38657433652).
The workflow and report upload completed successfully; the normal pipeline
job was skipped. The sanitized report records UTC day `2026-10-15`, verifies
the configured vault origin, `main` branch, and exact reservation checkout
path, and validates **4/4 inputs** and **7/7 durable ledgers**.

All seven ledgers are correctly classified as `prior_utc_day` and
`utc_day_rollover_pending_durable_reservation`; none was manually reset,
rewritten, or reconstructed. Their historical counts were 20, 20, 20, 18, 17,
17, and 17. The report therefore shows **0** already-current-day capacity and
**119** general attempts of conditional capacity after durable rollover (**17
per key**). The BTP reserve validation passed, preserving three requests per
key. The restore-only gate returned `COMPLETED` / `general_capacity_available`,
so it authorizes consideration of a separate bounded discovery run only; each
provider request still requires a successful durable reservation under the
general cap. No discovery was automatically chained.

The preflight dispatched **0** Tavily requests and created **0** reservations.
Applications, drafts, emails, WhatsApp messages, BTP actions, and production
candidate writes were all **0**. Only the sanitized report artifact was
uploaded.

The first CI run for the status-record commit exposed a rollover test that
hard-coded October 14/15, which became invalid after UTC midnight. The test was
changed to use dates relative to the current UTC day; the focused
Tavily budget-vault suite passed (**12 passed**), and Ruff, compilation, and
`git diff --check` passed. The correction was published in commit
[`9ab297f417a6b5e92da032ab31262d05b00d8a21`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/9ab297f417a6b5e92da032ab31262d05b00d8a21);
follow-up CI passed in [run 38658112630](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38658112630).

## Phase 2.8e bounded discovery-only evaluation (blocked before discovery)

Exactly one `discovery_only` workflow was dispatched on `main` after fresh
private-vault restoration was attempted:
[run 38658506355](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38658506355).
The sanitized `discovery-only-38658506355` artifact was retrieved. Its status
is `BLOCKED`, reason `provider_budget_ledger_missing_or_invalid`, phase
`input_restore`.

The report records **0** external results, normalized results, duplicates,
new records, research leads, rejected non-vacancies, individual vacancy
candidates, and verified vacancies. Provider API attempts, successes,
failures, retries, durable reservations, and deferred queries are all **0**.
No source or country/language coverage was reached; Emploi-Public listings and
details attempted, parsed, and verified were therefore all **0**. Eligibility
was `NOT_EVALUATED`. The run limits remained 12 combinations and 8 Tavily
attempts.

The workflow log confirms the private-vault restore step exited successfully
at the Actions step level, but the sanitized discovery report failed closed
before discovery. The report does not identify which ledger failed, nor
include the per-key dates/counts or the restore manifest, so the exact
ledger-level cause and current per-key balance cannot be established from
available sanitized evidence. The restore step suppresses restoration command
output and handles its failure by exiting successfully; thus the green step
does not independently prove restoration readiness. This is a reporting
limitation, not evidence that any ledger should be reset or bypassed.

This evaluation made **0** provider requests and **0** reservations, so it
created no budget changes. The report does not provide an exact post-run
per-key budget snapshot; no remaining budget is claimed. No follow-up
evaluation was dispatched. The normal/general pipeline, eligibility
assessment, Gmail, applications, drafts, email, WhatsApp, BTP workflows,
production candidate/application writes, and MEGEC retry were not run.
Only the sanitized report artifact was uploaded.

## Phase 2.8f fail-closed restore diagnostics

The Phase 2.8e failure mechanism is confirmed: the discovery restore shell step
used `set +e`, suppressed restore errors, and exited successfully on clone,
sparse-checkout, and checkout failures. The discovery CLI then blocked on the
missing/invalid input manifest, but neither that green step nor its sanitized
report identified the failing ledger. No current budget can be inferred from
that run.

The workflow now runs restoration with strict error handling, writes a
sanitized blocked report before attempting restoration, records stable
failure categories, and explicitly exposes and checks a `ready` output before
discovery can run. Restore-only and discovery-only reports upload even after
failure; cleanup removes private vault material and internal diagnostics.
Restoration reports now include each configured key's short fingerprint and
an allowlisted ledger outcome, without key values, ledger paths, or ledger
contents. Manifest validation checks restored-versus-expected input and ledger
counts. Offline tests cover restoration failures, invalid ledgers, count
mismatches, diagnostic sanitization, the successful no-write restore path,
workflow gating, and zero-dispatch behavior.

Local validation passed: **558 passed, 1 skipped** in the full test suite;
Ruff, Python compilation, workflow YAML parsing, and `git diff --check` passed.
The implementation was published in commit
[`d63c34913d4eb5b518428e96923f0f394629f313`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/d63c34913d4eb5b518428e96923f0f394629f313);
CI passed in [run 38662645254](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38662645254).

Exactly one post-fix `discovery_restore_preflight` was run:
[run 38662771165](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38662771165).
Its sanitized artifact confirms **4/4** required inputs and **7/7** budget
ledgers restored, with vault origin, `main` branch, and reservation checkout
path verified. The report's UTC day is **2026-10-15**. All seven ledgers were
classified `prior_day_rollover_pending`; none was reset or rewritten. The
three-request-per-key BTP reserve passed for all seven keys. The report shows
0 current-day general capacity and **119 conditional general attempts**
available after authorized durable rollover (17 per key). The discovery gate
authorized a separate bounded run, but this preflight itself made **0** Tavily
requests and **0** reservations and performed **0** application, outbound,
BTP, or production-write actions. No discovery was chained. This is not a
current-day ledger balance: the 119 attempts remain conditional on each
durable reservation succeeding in the discovery run.

## Phase 2.8g bounded discovery-only evaluation

Exactly one `discovery_only` workflow ran on current `main` at
[`2735af1bf283067f503be2b9c94e541eab5ca1d5`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/2735af1bf283067f503be2b9c94e541eab5ca1d5):
[run 38664200891](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38664200891),
on **2026-10-15 UTC**. The hardened discovery restore/readiness step
succeeded, validating the configured vault origin, `main` ref, reservation
checkout, all **4 required inputs**, and all **7 ledgers**. The restore
manifest passed its readiness gate, and the seven-ledger count is confirmed in
the sanitized run report. Prior-day ledgers rolled through successful durable
reservations only; no ledger was manually reset.

The run used **5/12** search-plan combinations and **8/8** permitted Tavily
attempts: 8 successes, 0 failures, 0 retries, 8 durable reservations, and 8
durable ledger writes. It deferred **38** additional provider requests at the
run-attempt cap. Ten source queries were attempted overall: 8 Tavily requests
and 2 Emploi-Public adapter queries (one direct Arabic query and one cached
French query). Tavily coverage comprised Morocco 5, Belgium 2, and France 1
attempt; languages were Arabic 3, French 3, Dutch 1, and English 1. The
dedicated Emploi-Public adapter reported 6 direct portal HTTP requests,
attributed to Arabic; its French query was cached. Other Tavily source
attempts were web search (3), official ATS search (2), Rekrute (1), Anapec
(1), and the Emploi-Public domain search (1). Other configured sources were
deferred by the run cap or disabled in source configuration.

The provider returned **76** search results; **40** were filtered (15
unrecognized official ATS hosts, 4 social domains, 21 outside configured
domain scope). The run fetched and normalized **39** results total, including
3 returned by the Emploi-Public adapter. There were **16 duplicates** and
**21 new discovery records**, all from Tavily: **16 research leads** and
**5 rejected non-vacancies**. All 21 new records had unverifiable posting
dates. No new individual vacancy was verified, and no new individual
vacancy candidate was reported as unverified; eligibility remained
`NOT_EVALUATED`, with owner review still required before any future
application decision.

Emploi-Public returned 3 normalized records: **2 procedural notices**
(1 `EXAM_CONVOCATION`, 1 `RESULTS_PUBLICATION`), neither matched to an
existing vacancy, plus one existing competition reverified as closed with
reason `application_deadline_elapsed`. No open competition was verified;
no cancellation was counted. The report records no unknown-status vacancy
or unverified vacancy candidate. It does not emit an ambiguous-submission-route
count. Its public-page counter combines `robots.txt`, listings, and detail
requests rather than separating those categories, so exact listing-versus-
detail attempts/fetches/parses cannot be recovered from this artifact.
Language attribution is also inconsistent: all 6 direct portal requests are
reported under Arabic, while the 2 parsed public-competition notices are
attributed to French. No language-specific listing/detail split is claimed.

After the run, the report showed **7 restored ledgers**, per-key general
capacity `[9, 17, 17, 17, 17, 17, 17]` (key identities intentionally not
included), **111 aggregate general attempts remaining**, and 0 run attempts
remaining. Each ledger stayed within the 17-request general limit, preserving
the 3-request-per-key BTP reserve. The remaining capacity is durable state
after the eight reservations; any further request still requires its own
successful reservation.

The sanitized artifact `discovery-only-38664200891` was uploaded and
retrieved. The bounded discovery step and artifact upload succeeded; restore
only ran for this task, and the general pipeline, employer-history and BTP
actions, normal vault restore, `.env` creation, WhatsApp steps, and vault
state persistence were skipped. The report records zero Gmail access,
applications, drafts, email, WhatsApp, and production database writes.
Only the eight budget-ledger reservations were written to the private vault;
no discovery run was retried.

## Phase 2.8h discovery precision improvements (offline only)

The Phase 2.8g sanitized artifact is now reconciled into this status entry.
The **76 provider results**, **39 normalized records**, **16 duplicates**,
**21 new discovery records**, and **0 verified open vacancies** are distinct
measures. The 21 records were 16 research leads and 5 rejected salary /
occupational references; they were not 21 vacancy candidates. The 38
deferrals were `run_attempt_cap`, not provider failures: all 8 allowed Tavily
attempts succeeded, and the 12-combination cap itself was not reached. The
allocator distributed requests across broad search, official ATS, and
Morocco-specific scopes; the fixed 8-request budget—not a broken connector—
prevented the other source/plan pairs from running. The report also counted
120 inapplicable country/source combinations, primarily because Morocco-only
sources were evaluated against international plan entries.

The official-ATS filter recorded 15 rejections, but the artifact intentionally
contains only deduplicated sanitized host/path examples, not all 15 result
URLs. Nine distinct rejected hosts are exposed: Bayt profile/job-board,
Bayt, Indeed, StepStone, Engineering.jobs, EnglishJobs.be/.no, Joobaz, and
the Sweco careers host; the remaining displayed example is blocked LinkedIn.
These are job-board/profile/careers/social hosts, not evidence of an
unrecognized ATS vendor endpoint. The artifact does not establish all
individual rejected host occurrences, and no raw URL/log evidence is
available in the sanitized artifact. No host was added to the ATS allowlist:
loosening it would admit aggregators or employer career pages as ATS results
without proving the application system or posting. Recognized ATS results
remain subject to detail verification, current-status, employer, date, and
route checks.

The 16 research leads comprised 5 collection/career-category pages and 11
`UNKNOWN` records. The artifact titles show clear occupational-reference,
discussion, legal/case, technology-project, and civil-protection false
positives alongside one explicitly labeled job-vacancy article. Deterministic
classification now rejects the identifiable non-employment references and
social/video pages, identifies free-job-post and job-description pages, and
keeps an explicitly titled vacancy as an **unverified individual-vacancy
candidate**. This does not establish that the article is current: posting
date remains unknown and the candidate must still pass official detail,
deadline, employer, and route verification. Salary/occupational references
continue to be rejected. Collection pages remain research leads rather than
vacancies.

The 8-request allocator now uses up to five distinct market-specific source
scopes while retaining coverage slots for broad search, official ATS, and an
unserved plan combination. Existing Morocco-only applicability and plan
language behavior are unchanged; tests assert Morocco French/Arabic plan
coverage, distinct domestic sources, the same 8-attempt limit, and accurate
run-cap deferrals. No daily limit, durable reservation, or three-request BTP
reserve was changed.

The Emploi-Public adapter now records robots checks separately from actual
listing/detail HTTP attempts, successful HTML responses, parsed listing pages
and cards, and parsed detail pages. Per-query-language counters are included
in the sanitized source report, so request language is no longer inferred
from the language of a parsed notice. Existing stage classification remains
separate for open/closed announcements, convocation, results, cancellation,
and unknown status. When a later procedural notice is observed, the existing
competition JSON now retains notice history and explicit supersedes links;
an older out-of-order notice is retained but cannot replace the newer state.
These are persistence-path changes covered by isolated test-database
fixtures; no production candidate or opportunity records were modified.

**Next evaluation proposal (not run):** only after a separately authorized
run and a fresh restore-only budget check, execute one `discovery_only`
evaluation with the unchanged ceilings of **8 Tavily attempts** and **12
search-plan combinations** (maximum budget request: 8 general reservations,
never from the protected BTP reserve). The revised allocator will favor up
to five distinct Morocco-specific scopes while retaining broad and ATS
coverage. Emploi-Public remains bounded by its configured 2 listing pages
and 12 detail pages, with robots checked first; each request remains
robots-aware. Expected evidence is attributable listing/detail telemetry,
official announcement stage, publication/deadline evidence, and submission
route status—not a guaranteed result count or verified vacancy. Fresh
preflight and successful durable reservation remain mandatory; this phase
did not read or mutate the private vault and makes no claim about its current
budget.

Local validation for Phase 2.8h passed: **560 tests passed, 1 skipped**;
Ruff, Python compilation, source/workflow YAML parsing, and `git diff --check`
passed. The implementation and reconciled Phase 2.8g results were published
in commit
[`e06d0ccea9b99e0bc36d94f400764a46bece80df`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/e06d0ccea9b99e0bc36d94f400764a46bece80df);
CI passed in [run 38687726344](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38687726344).
No live provider request, vault checkout, budget reservation, or production
write was performed during this implementation phase. No evaluation was
dispatched.

## Phase 2.8i restore-only readiness preflight

Exactly one fresh `discovery_restore_preflight` ran on `main` at the requested
Phase 2.8h commit
[`07f4f3eca3106f90b52b34b752be31a502ff26b5`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/07f4f3eca3106f90b52b34b752be31a502ff26b5):
[run 38689495219](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38689495219).
Its sanitized artifact was retrieved. The vault origin, `main` ref, and
reservation checkout path verified; **4/4 inputs** and **7/7 ledgers** were
restored. The report UTC day is **2026-10-15**. Two ledgers are valid for the
current UTC day and five are `prior_day_rollover_pending`.

The report shows **9 current general-purpose attempts** available: one
current-day key has 9/17 remaining; the other current-day key has exhausted
its 17-request general allowance, with its 3-request BTP reserve intact.
Another **85 attempts are conditional on durable rollover** across the five
prior-day ledgers (17 per key after a successful reservation-driven rollover).
These are not current-day balances and were not added to current capacity.
The report records 6 eligible keys (1 current-day plus 5 rollover-pending)
and 1 excluded general-discovery key. BTP reserve validation passed; the
configured reserve remains **3 requests per key**. The aggregate fields for
already current-day ledgers show 15 total requests remaining, split as 9
general and 6 reserved for BTP.

Restoration status was `COMPLETED`, reason `general_capacity_available`, and
the discovery gate field was authorized by budget policy. This indicates the
budget gate could admit a separate run subject to each durable reservation;
it is **not authorization to run discovery**. The preflight made **0**
provider requests and **0** reservations. The separate pipeline job was
skipped. No discovery or other downstream action was chained.

**Proposed plan for a separately authorized future evaluation (not run):**
keep the existing maximum of **12 unique plan combinations** and **8 Tavily
attempts**. Prioritize Morocco with six civil-engineering intents in French
and Arabic (three intent pairs): public competition/technician génie civil;
travaux publics, VRD, and chantier; and infrastructure, roads, and site
engineering. Use the remaining six combinations for justified international
coverage: France French (three corresponding intents), Belgium French (two),
and Belgium English (one). Reserve no more than eight Tavily attempts:
up to two broad Morocco French/Arabic searches; one official-ATS Morocco
search; and up to five distinct Morocco-specific scopes, prioritizing
ANAPEC, Emploi-Public indexing, Rekrute, Emploi.ma, and Amaljob. Actual
request allocation remains subject to cache, source applicability, and the
existing allocator; no cap is raised. The dedicated Emploi-Public adapter
continues to make only robots-aware requests within its existing bounds
(maximum two listing pages per language and 12 detail pages total); direct
portal page requests do not use Tavily reservations. Evaluate only official
detail evidence, current announcement status, explicit publication/deadline
evidence, and an unambiguous verified submission route. Search results and
snippets remain leads, not vacancy proof.

This is a readiness report only. No current budget was inferred from Phase
2.8g. Phase 2.8i made no source/configuration or safety-gate changes, and no
live evaluation was launched. The sanitized artifact is
`discovery-restore-preflight-38689495219`.

## Phase 2.8j one bounded discovery-only evaluation

After the separately authorized Phase 2.8i preflight, exactly one
`discovery_only` evaluation ran on `main` at
[`ae4ba9b80a138ad4f9e4e27d7bc0225ecfc777ca`](https://github.com/dubbishpsychic0000/ai-job-finder/commit/ae4ba9b80a138ad4f9e4e27d7bc0225ecfc777ca):
[run 38690026335](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38690026335).
The configured restore step and bounded discovery step succeeded, and the
sanitized artifact `discovery-only-38690026335` was uploaded and retrieved.
The restore-only job was skipped because this was the discovery task. The
general selected workflow, employer-history preflight, BTP workflow, general
artifacts/logs, vault persistence, WhatsApp steps, and related application/
outbound paths were skipped. The report records no Gmail access, applications,
drafts, email, WhatsApp, or production database writes. Its eight private-vault
writes were budget-ledger reservations only; runtime database writes were
ephemeral.

The report completed with **3/12 search-plan combinations** and **10 source
requests** attempted. It records **8/8 Tavily API attempts successful**, with
0 failures, retries, or rate-limit errors. Each of the eight attempts was
durably reserved before dispatch, with eight durable ledger writes; the
per-run attempt allowance was exhausted. The provider returned **71 results**,
of which **41 were filtered** (35 outside configured domain scope and 6
blocked social domains). Across the run, **33 results were normalized**,
including three Emploi-Public records, and **20 were duplicates**. The
remaining **11 new discovery records** are not verified vacancies: two were
research leads, two were rejected non-vacancies, and seven were individual
vacancy candidates whose current status remained unknown. All seven candidate
records had `ats_record_missing_required_job_evidence`; their dates were
classified `STALE_BY_POSTING_DATE`. Four other new records had
`UNVERIFIABLE_DATE`. No new vacancy was verified, and eligibility remained
`NOT_EVALUATED`.

Live search coverage comprised Morocco and France: Morocco had 9 recorded
search-country queries and France 1; recorded query-language totals were
Arabic 5 and French 5. The eight Tavily attempts comprised broad web search
(Morocco Arabic, France French), official-ATS search (Morocco French), and
Morocco-specific Emploi.ma (Arabic), Amaljob (French), ANAPEC (Arabic),
Emploi-Public indexing (French), and Indeed Morocco (Arabic). No Belgium
search was attempted. Exact search strings are not present in the sanitized
artifact, so source, country, and language scope—not verbatim query text—is
the reportable coverage. The report lists 38 source requests deferred by the
run-attempt cap. It also lists 8 `daily_request_budget_exhausted` events, but
per-key telemetry shows one such key skip and one successful reservation for
each of the 8 successful Tavily queries. These are not 8 additional
whole-query deferrals: the resilient connector skipped an exhausted key,
then served the request using another key, while the reporting layer
incorrectly counted the per-key skip as a query deferral. The report's 8
successes and 8 durable reservations remain the provider-request totals. The
artifact does not report the full adaptive plan or per-query
verification-field evidence.

The direct Emploi-Public adapter reported two requests (one direct and one
cached) and three normalized records, including two unmatched procedural
notices: one `EXAM_CONVOCATION` and one `RESULTS_PUBLICATION`. Neither is an
open vacancy. The existing competition recheck remained closed with
`application_deadline_elapsed`. Arabic telemetry recorded one robots request
and response, two listing-page attempts/fetches/parses, 26 parsed cards, and
three detail-page attempts/fetches/parses; French had zero direct portal page
requests. No open competition was verified.

After the run, all **7 budget ledgers** were reported restored, with
per-key general remaining `[0, 1, 17, 17, 17, 17, 17]`, **86 aggregate
general-purpose attempts remaining**, and zero run attempts remaining. The
per-key general cap remained 17 and the protected 3-request BTP reserve
remained intact. These are the report's post-run balances; further use still
requires a successful durable reservation for each provider request.

The run did not establish a currently open civil-engineering vacancy or
competition. The ATS candidates still require current official-detail
evidence, posting/deadline validation, and an unambiguous application route;
the run provides no eligibility determination. The reported 33 normalized
records are not 33 vacancies, and provider-result and normalization counts
must not be interpreted as yield. Phase 2.8j is complete; no discovery retry
or downstream action was authorized or launched.

## Phase 2.8k offline evidence-gap audit and query traceability

This audit used the complete retrieved sanitized report
`discovery-only-38690026335` and the verification/allocation/reporting code
published at `ae4ba9b`. No provider query, vault restore, reservation, or
production write was made.

### Seven ATS-hosted candidates

The artifact identifies all seven as SmartRecruiters-hosted listing URLs.
SmartRecruiters is the ATS host, not the employer-owned web domain. The
artifact includes posting titles, employer labels, locations, public detail
URL references, source-reported `releasedDate` values, `VACANCY_STATUS_UNKNOWN`
state, and the common reason
`ats_record_missing_required_job_evidence`. It does not preserve the ATS
payload, per-field verifier checks, source status value, deadline value, or
route-validation result. The `country`/`search_country` label is Morocco,
but `country_basis` explicitly says it was not independently verified; the
displayed work locations are outside Morocco.

| Stable record ID | Listing and reported location | Evidence and gaps from the artifact | Rule and next evidence |
|---|---|---|---|
| `record_668cda122a72a886` | Egis Group — “Technicien travaux OA H/F”; Caen, France; `releasedDate` 2026-06-17 | SmartRecruiters detail URL and posting ID are present; the ID passed the verifier's ID-match guard. Role/employer/location labels are present in the sanitized record, but their ATS-payload provenance is not recorded. Publication date is source-reported but stale (>45 days at the run); deadline and current status are not retained; no route is verified. | `VacancyDetailVerifier._smartrecruiters` → `_assess`; the required-evidence predicate returned `ats_record_missing_required_job_evidence`. Obtain per-field evidence flags and the official record's current status, substantive description, deadline/valid-through evidence, and posting-specific route. |
| `record_f2c74ebb2d2b71ed` | Artelia — “Technicienne / Technicien Etudes & Travaux en Infrastructures urbaines et VRD”; Cayenne, French Guiana; `releasedDate` 2026-08-26 | Same ATS-hosted detail/ID evidence and provenance gaps. Publication date is source-reported but stale (>45 days); no explicit deadline, closure, or verified route is in the artifact. Morocco is only the search-country label. | Same rule/reason. Retrieve verifier field checks plus an authoritative current status, current posting/deadline evidence, and posting-specific route; confirm geography before considering it relevant. |
| `record_08e56dd03f7633b8` | Ingérop — “TECHNICIEN ETUDES ET TRAVAUX F/H”; Abidjan, Côte d’Ivoire; `releasedDate` 2025-04-30 | Same ATS-hosted detail/ID evidence and provenance gaps. Publication date is source-reported and stale; deadline/current status/route evidence is absent from the artifact. | Same rule/reason. Retrieve the missing verifier checks and authoritative status/deadline/route; establish whether this old, non-Morocco posting still exists before any further research. |
| `record_a8a5e151d395cc89` | Spie batignolles — “Technicien Mécanicien TP - CSNE TOARC Secteur 1 - H/F”; Compiègne, France; `releasedDate` 2026-04-22 | Same ATS-hosted detail/ID evidence and provenance gaps. Source-reported publication date is stale; no deadline, explicit closure, current status, or verified route is retained. | Same rule/reason. Obtain verifier checks and authoritative current status, deadline/valid-through evidence, and application route. |
| `record_e71fa82ad86300fc` | SOCOTEC — “Technicien Ouvrage d’Art H/F”; Guyancourt, France; `releasedDate` 2026-07-28 | Same ATS-hosted detail/ID evidence and provenance gaps. Source-reported publication date is stale; no deadline/current status/verified route evidence is retained. | Same rule/reason. Obtain verifier checks, authoritative status and date evidence, plus a route tied to this posting ID. |
| `record_a64ff9d903a3392f` | Spie batignolles — “Technicien méthodes TP CSNE TOARC SECTEUR 1 - H/F”; Compiègne, France; `releasedDate` 2026-04-30 | Same ATS-hosted detail/ID evidence and provenance gaps. Source-reported publication date is stale; no deadline/current status/verified route evidence is retained. | Same rule/reason. Obtain verifier checks, authoritative current status/deadline evidence, and posting-specific route. |
| `record_d34727f047e3c151` | Veolia Environnement SA — “Technicien métreur / chiffreur travaux neufs eau et assainissement H/F”; Avon, France; `releasedDate` 2026-05-18 | Same ATS-hosted detail/ID evidence and provenance gaps. Source-reported publication date is stale; no deadline/current status/verified route evidence is retained. | Same rule/reason. Obtain verifier checks, authoritative status/deadline evidence, and posting-specific route; confirm geography. |

The reason code is emitted after SmartRecruiters JSON retrieval and an exact
posting-ID match; it is not evidence that all seven returned usable job
records. In `_assess`, the code returns this reason when the role/title is
missing or generic, the employer is missing or generic, the location is
missing, or the description is shorter than 80 characters or 10 words. The
artifact does not say which predicate failed for any individual candidate.
Because that check precedes route validation, the generic reason also does not
tell us whether a route was present or valid. The exact per-candidate missing
field therefore cannot be reconstructed from this historical artifact;
claiming that description alone caused all seven would be an inference.

All seven are **old by their source-reported publication dates** and have
**unknown current status**. None is classified as explicitly closed or
withdrawn, and the artifact contains no explicit expired deadline for these
seven. `STALE_BY_POSTING_DATE` is a freshness label only; it is not closure
evidence. The separately rechecked existing public competition was closed
with `application_deadline_elapsed`; it is not one of the seven new ATS
candidates.

The four `UNVERIFIABLE_DATE` records are separate and do not overlap the
seven individual vacancies: two job-board category pages, one salary/
occupational-information page, and one forum discussion. None has a
source-backed posting date. The two category pages are research leads; the
salary page and forum page were rejected as non-vacancies. The artifact does
not report their collection times as publication dates.

Their stable record IDs are `record_c795ea7bc123d9fa` (Sabbar category),
`record_a03e75e55967f3c1` (Naukri category),
`record_5289a80ebc82bb7a` (Go Construct salary/reference), and
`record_47b0e54b80ec7824` (Reddit discussion). They are not included in the
seven-candidate evidence-gap table or in the seven stale-posting count.

### Allocation and coverage reconciliation

The three reported search-plan combinations are distinct
`(query, country, language)` combinations that actually reached a connector;
12 was the upper bound, not a requirement to run 12. The 10 source requests
were not 10 Tavily API attempts: they comprise 8 live Tavily searches plus
the dedicated Emploi-Public adapter's 2 source queries (one direct, one
cached). The portal's six HTTP page requests (one robots check, two listing
requests, three detail requests) are page telemetry under those portal
queries—not additional search-plan combinations or Tavily attempts.

The source allocator fans requests across source scopes under the shared
eight-attempt budget. Its market-specific diversification can spend several
slots on Morocco-only sources, while broad and official-ATS sources are
country-universal. In this run, provider queries covered Morocco and France;
the artifact has no Belgium query. It has neither the ordered adaptive plan
nor the exact query-to-source allocation, so it cannot establish whether
Belgium was absent from restored preferences/the adaptive plan or lost to
allocation priority. It was not excluded by country applicability for the
broad or official-ATS sources, and Tavily cache hits do not explain its
absence.

The artifact's 8 `daily_request_budget_exhausted` “deferrals” are a separate
accounting defect: every one of the eight successful Tavily queries records
both an exhausted-key skip and a durable reservation. `ResilientTavily`
continues across keys after a per-key skip; `record_query_outcome` previously
treated `last_deferred_reason` as whole-query deferral even when a later key
successfully dispatched the query. Phase 2.8k only classifies a request as
deferred when no provider attempt occurred; skipped-key events remain
available in per-key budget telemetry. The 38 `run_attempt_cap` deferrals
are source/plan allocation requests not dispatched, not failed provider
calls. The old artifact's query deferral total therefore overstated whole
requests not attempted by eight.

### Traceability and evidence diagnostics added

The sanitized report schema is now version 5. It records the redacted
adaptive plan and a per-source row for each applicable source/plan request:
stable query-intent and plan-combination IDs, safe query text, intent,
country, language, source, `attempted`/`cached`/`deferred`/`failed` state,
reason code, result state, and actual provider-attempt count. Known profile
strings, emails, phone numbers, credential assignments, and URLs are removed
or replaced; public evidence URLs continue to lose query strings and
fragments. The report also gives every opportunity a stable ID derived from
its sanitized public URL.

ATS verification evidence now records booleans for employer, role, location,
description sufficiency, original detail-page reference and fetch, matched
posting ID, recognized current status, publication date, deadline, and
verified route.
These safe checks are paired with the sanitized public evidence URL, ATS
vendor category, outcome, and stable record ID; no job description or raw ATS
payload is included. Future sanitized reports can therefore identify the
specific failed evidence predicate instead of collapsing all candidates
under one reason. The freshness summary now separately counts stale
publication dates and explicitly elapsed deadlines. Existing stale records
remain unverified; no gate was relaxed.

### Validation and next-evaluation proposal

Regression tests cover query/profile/credential sanitization, stable query
IDs, source/plan deferral states, successful fallback after an exhausted key,
ATS evidence-field reporting, and stale-vs-expired dates. Validation completed
with these results:

- Focused regression tests: **9 passed**.
- Full test suite: **564 passed, 1 skipped**.
- Ruff on all changed Python files: **passed**. Repository-wide Ruff still
  reports 25 findings in untouched files; those unrelated baseline issues were
  not changed as part of this phase.
- Python compilation, parsing of the 5 relevant workflow/config YAML files,
  and `git diff --check`: **passed**.
- Implementation commit `073d725681fc1297419efc693d2b1c15f253b3c3` was
  published to `main`; its CI run
  [38881147937](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38881147937)
  passed (test job completed successfully).

For a later separately authorized evaluation, keep the existing limits of
12 search-plan combinations and 8 Tavily attempts, with a proposed explicit
allocation of **6 Morocco attempts and 2 international attempts**. Use the
Morocco slots for two broad civil-engineering queries (French and Arabic),
one Morocco official-ATS query, and three distinct high-priority local scopes
(ANAPEC, Emploi.ma, and Rekrute or Amaljob according to current source
priority). Use the two international slots explicitly for one France and one
Belgium query, choosing the appropriate language and broad/official-ATS
source. Keep the dedicated Emploi-Public adapter separate from Tavily's
attempt cap and within its robots-aware page limits. Record its query and
page telemetry independently. The 8 provider attempts remain the maximum;
each still needs a successful durable general-purpose reservation that
preserves the protected BTP reserve. Do not infer coverage from the plan:
the next report must show planned versus attempted, cached, deferred, and
failed source/plan rows, plus evidence checks for every verified candidate.

This is an offline audit and implementation only. No new live discovery,
provider request, vault restore/write, budget reservation, application,
message, BTP action, or production candidate/opportunity mutation was made.

## Phase 2.8l fresh restore-only budget preflight

Exactly one `discovery_restore_preflight` ran on `main` at Phase 2.8k status
commit `31cd8acdef459099c04553b1c78be9bcf61dfd58`:
[run 38907638754](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38907638754).
The job and sanitized report upload succeeded; the normal `pipeline` job was
skipped. The report was retrieved as
`discovery-restore-preflight-38907638754`.

At the preflight timestamp the UTC day was **2026-10-16**. The configured
private-vault origin, `main` ref, restored clone path, and reservation checkout
path were verified. All **4/4 required inputs** and **7/7 expected budget
ledgers** were present and validated. Ledger outcomes were **3
`valid_current_day`** and **4 `prior_day_rollover_pending`**.

Current general-purpose capacity is **0**. Three current-day ledgers each
have a general count of 17/17; their remaining **9 total requests** are the
protected BTP reserve (3 per key), not discovery capacity. The BTP reserve
validation passed and every ledger reports `btp_reserve_intact: true`.

Four keys are conditionally eligible for general discovery after a successful
durable reservation performs UTC-day rollover: fingerprints `240c8071`,
`bf109685`, `a20550bf`, and `39950b91`. Each would have at most 17 general
attempts after rollover, or **68 conditional attempts total**. This is not
current capacity or a reservation; the preflight created **0** reservations.
The three excluded keys are fingerprints `da6f156a`, `95f8557c`, and
`3265acb4`; their current-day general allowance is exhausted, while their
three-request-per-key BTP reserve remains protected.

The proposed allocation of six Morocco and two international Tavily attempts
is within the conditional 68-attempt general capacity and the existing
eight-attempt cap, but is **not currently dispatchable**: each request still
requires its own successful durable reservation after rollover. The existing
12-plan-combination limit is unchanged. Source applicability supports the
intended country split: broad Tavily and official-ATS search apply to all
countries; Morocco-only job-board and government sources apply only to
Morocco. The Emploi-Public adapter remains Morocco-only and separate from
Tavily's attempt count, with existing bounds of 2 listing pages and up to 12
detail pages, subject to robots checks. Preflight made no portal requests.

The report's `discovery_gate.authorized` value reflects conditional
reservation-gated eligibility, not authorization from the user to evaluate.
This preflight dispatched **0** provider requests and **0** discovery queries;
it made **0** reservations and **0** application, messaging, BTP, or
production-write actions. No discovery was chained. The proposed allocation
remains subject to a separate explicit authorization and successful durable
reservations at dispatch time.

## Phase 2.8m one bounded discovery-only evaluation

Following explicit authorization, exactly one `discovery_only` workflow ran
on `main` at commit
`2129af7a8afd35a3ff5e8f84ceb7bf4441999edc`:
[run 38910729695](https://github.com/dubbishpsychic0000/ai-job-finder/actions/runs/38910729695).
The sanitized artifact `discovery-only-38910729695` uploaded successfully and
was retrieved. The restore step and bounded discovery command succeeded. The
normal application pipeline, employer-history and BTP workflows, WhatsApp,
vault state persistence, and non-sanitized run-log upload were skipped.

### Readiness, reservations, and remaining budget

The discovery restore/readiness gate passed on UTC day **2026-10-16**. All
**4/4 required inputs** and **7/7 budget ledgers** were restored and validated;
the private-vault origin/ref and reservation checkout path passed the existing
checks. The three current-day ledgers had no general allowance. The other four
were rolled over only through the durable reservation mechanism: all **8/8
Tavily attempts** received a successful durable general-purpose reservation
before dispatch, with **8 durable ledger writes**. There were **0** failed
reservations, provider failures, rate-limit errors, or retries.

The report's post-run general-purpose budget (limit **17 per key**, excluding
the protected three-request-per-key BTP reserve) is **60 attempts remaining**,
reported per key as `[0, 0, 0, 9, 17, 17, 17]`. This is the sanitized
general-budget summary; it does not expose a fingerprint-to-position mapping.
Discovery used only the general allowance. The BTP reserve remained excluded
by the durable reservation limit and no BTP request or action occurred.

### Actual query allocation and deferred requests

The plan contained **12** combinations, of which **3** reached at least one
connector: Morocco Arabic `[profile_term] technician`, Morocco French
`technicien travaux publics`, and France French `[profile_term] technician`.
The exact redacted query text, stable intent/combination IDs, source state,
attempt count, result state, and reason are preserved in the sanitized
artifact's query traces.

Tavily allocation did **not** achieve the requested six-Morocco/two-
international target. It made **7 Morocco attempts** (4 Arabic, 3 French),
**1 France attempt** (French), and **0 Belgium attempts**. Belgium's planned
Dutch-language role query `[profile_term] technician` and English-language
`junior [profile_term] technician` each had both broad web and official ATS
source requests deferred with `run_attempt_cap` after the shared attempt cap
was consumed; the broad and ATS sources were applicable to Belgium.
Morocco-only sources were, as configured, inapplicable outside Morocco.

The eight live Tavily attempts were: Morocco Arabic broad search, Rekrute,
Emploi.ma, and ANAPEC (**4**); Morocco French official ATS, Emploi-Public
domain search, and Indeed Morocco (**3**); and France French broad search
(**1**). The four cached rows were the Emploi-Public-domain Tavily scope,
Marocannonces, Indeed Morocco, and the Emploi-Public adapter.

| Source | Tavily attempts | Cached source responses | Deferred source/query requests |
|---|---:|---:|---:|
| Broad web search | 2 | 0 | 10 |
| Official ATS search | 1 | 0 | 11 |
| Rekrute | 1 | 0 | 1 |
| Emploi.ma | 1 | 0 | 1 |
| Amaljob | 0 | 0 | 2 |
| ANAPEC | 1 | 0 | 1 |
| Emploi-Public-domain Tavily scope | 1 | 1 | 0 |
| Marocannonces | 0 | 1 | 1 |
| Dreamjob | 0 | 0 | 2 |
| Indeed Morocco | 1 | 1 | 0 |
| Bghit Nekhdem | 0 | 0 | 2 |
| Jobzyn | 0 | 0 | 2 |
| Moncallcenter | 0 | 0 | 2 |

The **35 deferred source/query combinations** all have reason
`run_attempt_cap`; the per-source counts above reconcile to the artifact's
`requests_not_attempted_due_to_budget: 35`. Across the source traces there
were **13** attempted-or-cached source requests: 8 live Tavily requests, 1
direct Emploi-Public adapter query, and 4 cached responses. The adapter's
cached query is not a Tavily attempt. No query/source request failed.

The robots-aware Emploi-Public adapter made **7 portal page requests**, all
in Arabic: 1 robots request, 2 listing-page requests (both fetched and
parsed), and 4 detail-page requests (all fetched and parsed). It parsed 26
listing cards and 2 procedural notices: one convocation and one results
publication, neither an open-vacancy confirmation. One existing competition
was reverified and reported closed with reason
`application_deadline_elapsed`; no new public competition was verified.

### Discovery and verification outcomes

The report records **72 provider results returned**, **48 filtered** (24
from cached provider results), **28 external results fetched**, **28
normalized records**, and **17 duplicates**. These are not vacancy counts.
Among **9 new records**, the classification was **6 research leads**, **2
rejected non-vacancies**, and **1 unverified individual-vacancy candidate**;
all 9 had `UNVERIFIABLE_DATE`. There were **0 verified open vacancies** and
**0 newly proven expired/closed vacancies**.

| Record | Employer | Role | Location | Detail/posting identity | Current status | Publication/deadline | Application route |
|---|---|---|---|---|---|---|---|
| `record_5c2f94d85b303720` | Unknown | “Job Vacancy: Apprentice Civil Engineering Technician” | Morocco is only the unverified search-country label | A public listing reference exists at `https://teacakeclub.org/2026/08/19/job-vacancy-apprentice-civil-engineering-technician`; the source is unsupported for detail verification, no detail fetch or posting identity was verified | Unverified; current status not established; reason `unsupported_detail_verification_source` | Publication date and deadline unknown; URL date is not accepted as posting-date evidence | `NOT_DETECTED`; no route verified |

This candidate is **not proven open, old, expired, or closed**. Its source
reference and title do not establish employer, actual work location,
posting-specific identity, current status, publication date, deadline, or
submission route. Eligibility was not evaluated. The sanitized artifact
records the available evidence and verification reason; no snippet or HTTP
success was treated as vacancy proof.

### Side effects and stop

The sanitized report records **0** production-database writes, applications,
drafts, emails, Gmail access, or WhatsApp messages. The workflow skipped BTP
and all downstream application paths. It made **8 authorized private-vault
budget-ledger writes** for reservations; production candidate/opportunity
records were not changed. Discovery-result persistence was limited to the
ephemeral run database. No retry or second evaluation was launched.

## Phase 2.8n offline allocation and source-evidence audit

This phase is offline implementation, regression testing, and documentation
only. It does not include a new restore, reservation, provider/portal request,
live discovery evaluation, application action, or production candidate or
opportunity write. The audited reference remains Phase 2.8m run
[38910729695](https://github.com/actions/runs/38910729695).

### Allocation root cause and correction

The 2.8m run's shared Tavily run cap was enforced globally, while query
combinations were scheduled per source and source connectors ran in order.
Country intent was therefore not protected at reservation time: earlier
Morocco requests used seven of eight attempts, France used one, and Belgium
used none. The 12 plan combinations, source/query requests, cached responses,
Emploi-Public page requests, and Tavily HTTP attempts are separate units and
must not be conflated.

The bounded discovery workflow now gives the shared attempt budget explicit
country limits of **6 Morocco, 1 France, and 1 Belgium**, in addition to the
existing **8 total Tavily-attempt** and **12 plan-combination** limits.
Country capacity is charged only after the per-request durable general-budget
reservation succeeds, immediately before provider dispatch. A Morocco request
cannot consume France's or Belgium's allocation. Cache hits and per-key
budget skips do not consume country or run attempt capacity; provider retries
are separately counted as actual attempts. Reservation failures continue to
block dispatch. Morocco-only source scopes remain Morocco-only, while broad
web and official-ATS scopes can serve the international plan combinations.
If a country has no eligible uncached source request or its reservation fails,
the slot stays unused and the report gives the reason; country allocation
does not override daily budgets or use the protected BTP reserve.

Sanitized reports now show Tavily planned, eligible, attempted, cached,
deferred, failed, and unavailable source/query counts by country and source,
plus country targets, actual reserved attempts, remaining slots, target
achievement, and unfilled-slot reasons. Actual provider-attempt counts—not
the target—determine whether the country allocation was achieved. The
robots-aware Emploi-Public adapter is kept out of Tavily attempt totals and
retains its separate portal telemetry. Query traces continue to include safe
normalized query text, stable intent/combination IDs, country, language,
source, outcome, and reason; sensitive profile terms and credentials remain
redacted. An `eligible` trace row means the source/country applied, the source
was configured, the query was uncached, and general plus country capacity was
available when the report was initialized; runtime deferrals still record the
later reservation or cap outcome. These report additions use schema version 6.

### Unsupported candidate disposition

The 2.8m record `record_5c2f94d85b303720` is a search result with a
vacancy-shaped title at `teacakeclub.org`, not a supported official employer
or ATS record. The artifact does not establish a named employer, actual
location, posting-specific identity, current status, publication date,
deadline, or application route. Its `unsupported_detail_verification_source`
reason reflects that the detail-verification adapter could not verify this
host; it does not prove that all seven candidates in the earlier 2.8j
evidence-gap audit came from supported ATS sources. No page was fetched and
the URL's date-shaped path is not treated as date evidence.

The classification rule now keeps a generic `Job Vacancy:` search result
from an unsupported host with no identifiable employer as
`UNKNOWN` / `RESEARCH_REQUIRED` (`unsupported_vacancy_source`), rather than
counting it as an individual-vacancy candidate. It remains a research lead,
not an explicitly closed, withdrawn, expired, or open vacancy. Trusted
official sources could corroborate or refute its claims only in a later,
separately authorized discovery evaluation; no source trust or verification
gate was broadened.

### Next-evaluation plan and limits

For a separately authorized future evaluation, retain at most 12 search-plan
combinations and 8 Tavily provider attempts, with country attempt allocations
of 6 Morocco, 1 France, and 1 Belgium. Favor Moroccan French/Arabic
civil-engineering vacancies and official public competitions, using broad
and official-ATS coverage where relevant and Morocco-only sources only for
Morocco. Keep the France and Belgium slots explicit, using applicable
internationally eligible sources and appropriate French/Dutch/English plan
queries. Cached combinations should be recorded but followed by another
eligible uncached query in that country's remaining allocation when
available. Do not silently shift an inapplicable, unavailable, cached-only,
budget-skipped, or failed country's slot to another country. Each live
provider attempt still requires its own successful durable general-budget
reservation. Emploi-Public portal requests remain separately counted under
its current robots-aware limits.

No live run is authorized by this audit or by an allocation-ready result.
Any later evaluation requires fresh readiness verification and separate
explicit authorization.

### Validation

- Focused allocation, classification, sanitized-report, and durable-reservation
  regression tests: **69 passed**.
- Additional actual Tavily-dispatch cap and bounded-workflow allocation-wiring
  tests: **passed**.
- Full test suite: **570 passed, 1 skipped**.
- Ruff on all changed Python files, Python compilation, parsing of all
  **8 workflow/config YAML files**, and `git diff --check`: **passed**.

## Phase 2.8o restore-only readiness check — blocked

The requested checkout was verified at `main` commit
`5f3df36068e14a6df1e00a52246b8886949c73c8`. The preflight workflow was
inspected: its dedicated restore-only job reads and validates the private
vault and budget ledgers, reports zero reservations and zero provider
requests, and excludes the normal pipeline job.

Exactly one dispatch was attempted for `discovery_restore_preflight`, but
GitHub rejected the workflow-dispatch API request with HTTP 403:
`Must have admin rights to Repository`. No Actions run was created, so there
is no preflight run URL or sanitized readiness artifact. No vault restore,
ledger read, reservation, Tavily/portal/provider request, or downstream job
ran in this phase.

The current UTC date at the authorized check time was **2026-10-17**. Because
the preflight did not execute, today's required-input and ledger validation
counts, current general-purpose capacity, conditional rollover capacity,
eligible/excluded provider keys, and three-request-per-key BTP reserve status
are **unverified**. The prior 2026-10-16 preflight is historical and must not
be used to infer today's availability. The proposed 6 Morocco / 1 France / 1
Belgium allocation remains within the published 8-attempt and 12-plan
combination caps, and broad web / official-ATS sources remain configured for
international countries while Morocco-specific sources remain restricted to
Morocco; budget feasibility cannot be confirmed without the blocked fresh
preflight. No readiness or live-evaluation authorization is implied.

Safe remediation is to have a repository administrator grant the required
workflow-dispatch permission, then request a new, separately authorized
restore-only preflight for the current UTC day. No live discovery evaluation
should be started before that check and separate explicit authorization.
