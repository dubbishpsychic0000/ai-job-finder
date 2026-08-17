# Worldwide Career Agent

A 24/7 autonomous AI agent that discovers international job opportunities,
matches them to your profile, scores them, decides what to do, and — only
through a deterministic safety gate — contacts employers.

Implemented from the architecture spec as a **scraper-is-the-eyes** pipeline:

```
Discovery → Normalization → Deduplication → Analysis → Scoring → Decision → Action → Memory
```

## Design highlights

- **Connectors as eyes** — every source (RSS, company careers page, web search, or a
  custom/static feed) emits the same `Opportunity` structure behind one interface.
  Adding a site later means writing one adapter; nothing else changes.
- **AI Brain as reasoning** — specialized agents (Job Analyst, Candidate Matcher,
  Mobility, Immigration, Decision, Communication). LLM provider is swappable; the
  default `null` driver runs the whole pipeline offline with deterministic heuristics.
- **Quota-aware resilience** — multiple Gemini API keys form a rotation ring:
  each key gets its own daily budget (default 20 = free tier), successful
  responses are cached on disk (re-runs cost 0 extra calls), and any
  failure/quota-exhaustion (429) skips that key for the day, moves to the next,
  then degrades gracefully to the offline heuristics — the pipeline never
  stalls because an API key is busy.
- **Engineered scoring, not vibes** — the LLM emits component sub-scores; a
  deterministic weighted engine combines them into the six reported dimensions and
  the overall score. Thresholds live in `config/settings.yaml`.
- **Rules first, then AI** — hard guards (already processed, stale postings,
  impossible experience requirements, low score) decide before the model gets a vote.
- **Email Safety Gate** — deterministic checks only the model can't self-verify:
  valid recipient, contact cooldown, **no invented claims** (checked against the
  candidate profile allowlist), fresh posting, real CV attachment, daily rate caps.
  A failing check blocks the send and is recorded for the dashboard.
- **Evidence-backed immigration** — claims are only accepted from whitelisted official
  government domains, and every claim stores `{claim, source, verified_at}`.
- **Memory in a relational DB** — SQLite for dev, PostgreSQL via `DATABASE_URL`.
  Follow-ups are a state machine with hard caps, not a spam loop.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows           (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

wca init                          # create the database
wca run-once                      # full pipeline: discover -> analyze -> decide -> act
wca stats                         # dashboard-style summary
wca dashboard                     # web dashboard at http://127.0.0.1:8000
```

`wca run-once --json` emits machine-readable output. Other commands:
`discover`, `analyze`, `act`, `followups`, `search-plan`, `pause`, `resume`,
`scheduler`.

### Safety defaults

- **Email is OFF by default.** Three `EMAIL_MODE` levels, each storing every
  communication and its outcome (nothing is ever silently dropped):
  - `EMAIL_MODE=dry_run` — generate + verify the email locally, record it, and
    **never** touch Gmail. Uses no daily budget.
  - `EMAIL_MODE=draft` **(default)** — the agent prepares a real **Gmail Draft**
    (never sent) that a human reviews and sends from `gmail.com`.
  - `EMAIL_MODE=live` — actually send, but only after the Safety Gate approves.
  - With `ENABLE_EMAIL=false`, draft/live interaction is refused and recorded as
    `blocked`; dry-run still works (it is fully local and safe).
- **Your identity never leaves your machine.** `candidate/cv/*.pdf`,
  `candidate/profile.yaml`, `candidate/preferences.yaml`, `.env` and
  `secrets/` are **git-ignored** — a fresh clone provides its own profile, CVs,
  keys and OAuth files. Emails are signed with `name`, `title` and `phone`
  (all read from `candidate/profile.yaml`, never guessed), and the attachment
  carries your name: `CV_<Name>_FR.pdf` / `CV_<Name>_EN.pdf` / `CV_<Name>.pdf`.
- The **demo connector** (`config/sources.yaml`, `static_files` onto the test
  fixtures) runs offline so you can verify behaviour with zero external access.
- Real web connectors (RSS, search) are ships disabled; enable them deliberately.

## Enable live operation

1. `cp .env.example .env` and fill in:
   - `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` (or bare `GEMINI_API_KEYS=key1,key2,...`
     to rotate across several keys when one hits its daily quota). **Use a
     `-latest` model alias** (`GEMINI_MODEL=gemini-flash-latest`) — the
     `gemini-2.x` names are deprecated for new keys. A full run costs ~50 Gemini
     calls, so keep `LLM_DAILY_BUDGET=20` (free tier) — anything beyond the
     keys' combined budget runs on the offline heuristics automatically.
   - `EMAIL_MODE` and `EMAIL_PROVIDER=log|smtp|gmail`, plus SMTP/Gmail credentials.
   - Outbound policy: `DAILY_MAX_APPLICATIONS`, `DAILY_MAX_INQUIRIES`,
     `DAILY_MAX_OUTBOUND`, `EMPLOYER_COOLDOWN_DAYS`, `MIN_APPLICATION_SCORE`,
     `MIN_APPLICATION_CONFIDENCE` (see `EMAIL_MODE`/limits below).
   - Your CV(s) into `candidate/cv/` (see `candidate/cv/README.txt`).
2. Update `candidate/profile.yaml` and `candidate/preferences.yaml` — these are the
   **only** facts the agent may claim.
3. Enable the real connectors in `config/sources.yaml` (RSS feeds, career pages).

### Email rollout checklist (safe)

```bash
# 1) dry-run: every action just gets validated + recorded, zero Gmail contact
wca run-once            # EMAIL_MODE=dry_run
wca email-policy        # see the exact limits in force right now
# 2) Gmail drafts: get a token, then actually prepare drafts
wca gmail-auth          # OAuth; opens consent in a browser, token -> secrets/gmail_token.json
#   NOTE: the flow uses the FIXED redirect URI http://localhost:18320/ — add that
#   exact value under the OAuth client's "Authorized redirect URIs" (Google Cloud
#   Console > Credentials) or you'll get an immediate redirect_uri_mismatch 400.
#   An unverified app also needs your Gmail added as a Test user (Consent screen).
#   .env: EMAIL_MODE=draft   ENABLE_EMAIL=true
wca act                 # inspect the drafts you now create
wca dashboard           # review each drafted email, then Send manually
# 3) live (only when you've reviewed real drafts for a few cycles)
#   .env: EMAIL_MODE=live    ENABLE_EMAIL=true
# panic stop from the CLI or REST:  wca pause    (or POST /api/pause)
```

The communication agent is explicitly forbidden from inventing experience,
qualifications, certifications, employers, projects, languages, visa status or
salary history; the safety gate refuses to send anything that violates this.
An **auto-apply guard** also forbids APPLY below `MIN_APPLICATION_SCORE` /
`MIN_APPLICATION_CONFIDENCE` no matter what the model decides, and the global
`wca pause` switch halts every outbound (draft or live) instantly.

## Testing

```bash
wca init
pytest -q
```

The golden-set tests pin the exact decisions the demo fixtures must produce, so
a prompt/model tweak cannot silently flip actions. They run against a **fixed
test profile** (`tests/fixtures/profile.yaml`) — never your live
`candidate/profile.yaml` — so editing your CV can't break the guardrails. Dedup,
scoring, safety-gate and immigration-whitelist behaviour are covered by unit tests.

## Deployment

```bash
docker compose up --build          # app + postgres + dashboard
```

The app container runs `wca run-once` on a loop (or point cron at
`wca run-once`); `dashboard` exposes the web UI on `:8000`.

## Project layout

```
app/
  agents/        job_analyzer, candidate_matcher, mobility, immigration, decision, communication, llm, llm_resilience
  connectors/    base interface + rss / company_careers / search_engine / static_files + immigration/official
  api/           FastAPI dashboard + endpoints
  email/         ApplicationEngine + Safety Gate + providers (log/smtp/gmail)
  memory/        repository layer on PostgreSQL/SQLite
  scoring/       deterministic aggregation
  workflows/     discovery / analysis / action / followup / pipeline / search_plan
  scheduler/     loop + pause/resume control
candidate/       profile.yaml, preferences.yaml, cv/
config/          settings.yaml (thresholds, weights, rules), sources.yaml (connectors)
tests/           golden-set + unit tests, fixture feeds
```