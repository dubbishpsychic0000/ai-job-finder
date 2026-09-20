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
- Official email verification requires the address domain to match the
  employer domain; ATS vendor and free-mail addresses remain unverified.
- CI preserves Tavily and LLM budget/cache state between vault-backed runs.

## Remaining investigation

The site researcher, evidence store, employer seed sources, ATS tenant
registry, structured text extraction, and coverage evaluation harness are not
implemented by this hardening pass. They should be added incrementally with
robots/terms checks and a measured golden set of known employers.
