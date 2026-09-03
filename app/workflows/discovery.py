"""Discovery workflow — runs the configured connectors across the search plan,
normalizes, deduplicates against memory and stores only genuinely new jobs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, Preferences, load_yaml
from app.connectors import registry
from app.connectors.tavily_resilience import DailyBudget, TavilyKey, key_fingerprint
from app.deduplication import find_duplicates
from app.discovery.email_verification import EmailVerificationService
from app.discovery.opportunity_details import classify_opportunity, detect_application_method
from app.discovery.verification import freshness_label
from app.discovery.vocabulary import CandidateVocabulary
from app.normalization import normalize

logger = logging.getLogger(__name__)

DEFAULT_SOURCES_PATH = ROOT_DIR / "config" / "sources.yaml"
DEMO_SOURCES_PATH = ROOT_DIR / "config" / "sources_demo.yaml"


@dataclass
class DiscoveryReport:
    combinations_attempted: int = 0
    opportunities_fetched: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    source_errors: list[str] = field(default_factory=list)
    employers_discovered: int = 0
    immigration_facts: int = 0
    opportunity_sources: int = 0
    social_signals: int = 0
    # One entry per configured connector.  This makes a zero-result run
    # explainable without treating it as an error.
    source_reports: list[dict] = field(default_factory=list)


def _load_source_connectors(path: Path | None = None) -> list[tuple[dict, object]]:
    path = path or DEFAULT_SOURCES_PATH
    cfg = load_yaml(path)
    import os
    from dotenv import load_dotenv
    # Ensure .env is loaded into os.environ so downstream modules see TAVILY_API_KEYS
    load_dotenv()
    proxy = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None

    # Build Tavily resilient keys from TAVILY_API_KEYS (comma-separated)
    tavily_keys_str = os.getenv("TAVILY_API_KEYS", "")
    tavily_keys = [k.strip() for k in tavily_keys_str.split(",") if k.strip()]
    tavily_resilient_keys = []
    for i, k in enumerate(tavily_keys):
        budget_path = Path(f"data/tavily_budget_{key_fingerprint(k)}.json")
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        tavily_resilient_keys.append(TavilyKey(
            api_key=k,
            budget=DailyBudget(budget_path, limit=20),
            fp=key_fingerprint(k),
        ))

    connectors = []
    for c in cfg.get("connectors", []):
        if not c.get("enabled", True):
            continue
        kind = c["kind"]
        if kind not in registry:
            logger.warning("Unknown connector kind %s — skipping", kind)
            continue
        opts = _resolve_paths(c)
        opts.pop("kind", None)
        opts.pop("name", None)
        opts.pop("enabled", None)
        opts.pop("mode", None)  # metadata for diagnostics, not connector options
        if kind in ("search_engine", "tavily"):
            opts["source_name"] = c.get("name", kind)
            if proxy:
                opts["proxy"] = proxy
        if kind == "tavily" and tavily_resilient_keys:
            # Use resilient wrapper with all keys
            from app.connectors.tavily_resilience import ResilientTavily, TavilyCache
            cache = TavilyCache(Path("data/tavily_cache.json"))
            connector = ResilientTavily(
                keys=tavily_resilient_keys,
                cache=cache,
                results_per_query=c.get("results_per_query", 10),
                official_only=c.get("official_only", False),
                domains=c.get("domains"),
                source_name=c.get("name", "tavily"),
                result_source_type=c.get("result_source_type", "search_engine"),
            )
            connectors.append((c, connector))
            continue
        try:
            connectors.append((c, registry[kind](**opts)))
        except Exception as exc:
            logger.warning("Failed to init connector %s: %s", c.get("name"), exc)
    return connectors


def _resolve_paths(cfg: dict) -> dict:
    opts = dict(cfg)
    p = opts.get("path")
    if p and not Path(p).is_absolute():
        opts["path"] = str(ROOT_DIR / p)
    return opts


async def run_discovery(session: Session, config: AgentConfig, prefs: Preferences,
                        sources_path: Path | None = None,
                        profile: CandidateProfile | None = None,
                        llm=None) -> DiscoveryReport:
    report = DiscoveryReport()
    discovery_cfg = config.discovery or {}
    max_per_country = int(discovery_cfg.get(
        "max_per_country", config.search_plan.get("max_combinations_per_country", 3)))
    max_queries = int(discovery_cfg.get("max_queries_per_run", 40))
    vocab = CandidateVocabulary(profile=profile, prefs=prefs)
    # Optional LLM vocabulary expansion (discovery.vocab_llm) — cached on disk.
    if llm is not None and discovery_cfg.get("vocab_llm", False):
        await vocab.llm_expanded_roles(llm)
        logger.info("vocab_llm enabled; vocabulary now %d terms", len(vocab.roles()))
    from app.workflows.adaptive_plan import build_adaptive_plan

    connectors = _load_source_connectors(sources_path)
    plan = build_adaptive_plan(session, prefs, config, vocab=vocab, profile=profile,
                               max_per_country=max_per_country,
                               max_queries_per_run=max_queries,
                               learning_sources={c.get("name", connector.name)
                                                 for c, connector in connectors})

    # Employer + agency universe (§7, §9) — opt-in so the job pipeline stays hermetic.
    if discovery_cfg.get("employer_discovery", False):
        from app.workflows.employer_discovery import run_employer_discovery

        ereport = await run_employer_discovery(session, config, prefs, profile=profile)
        report.employers_discovered = ereport.stored

    # Immigration & work-pathway discovery (§11, §13) — opt-in, official web sources.
    if discovery_cfg.get("immigration_discovery", False):
        from app.workflows.immigration_discovery import run_immigration_discovery

        ireport = await run_immigration_discovery(session, config, prefs, profile=profile)
        report.immigration_facts = ireport.stored

    # Search-engine expansion: surface opportunity sources (§6) — opt-in, live web.
    if discovery_cfg.get("opportunity_source_discovery", False):
        from app.workflows.opportunity_sources import run_opportunity_source_discovery

        os_report = await run_opportunity_source_discovery(session, config, prefs, profile=profile)
        report.opportunity_sources = os_report.stored

    # Authorized social signals: LinkedIn (index) + Meta/user-provided (§15-18).
    if discovery_cfg.get("social_signal_discovery", False):
        from app.workflows.social_signals import (
            DEFAULT_SOCIAL_SOURCES_PATH,
            run_social_signal_discovery,
        )

        spath = Path(discovery_cfg.get("social_sources_path")) if discovery_cfg.get(
            "social_sources_path") else DEFAULT_SOCIAL_SOURCES_PATH
        sreport = await run_social_signal_discovery(session, config, prefs,
                                                    sources_path=spath, profile=profile)
        report.social_signals = sreport.stored

    for source_cfg, connector in connectors:
        source_name = source_cfg.get("name", connector.name)
        source_report = {
            "name": source_name,
            "kind": source_cfg.get("kind", getattr(connector, "kind", "?")),
            "mode": source_cfg.get("mode", "real"),
            "queries": 0,
            "fetched": 0,
            "normalized": 0,
            "duplicates": 0,
            "new": 0,
            "error": "",
            "rate_status": "ok",
        }
        source = mem.store.upsert_source(session, source_name,
                                         getattr(connector, "kind", "?"), str(source_cfg.get("path", "")))
        items_found = 0
        max_requests = int(discovery_cfg.get("max_requests_per_source", 0))  # §25 per-source budget
        combos = plan[:max_requests] if max_requests else plan
        parallel = bool(discovery_cfg.get("parallel_fetch", False))  # §26 — concurrent searches

        def process(combo, results, source_name) -> int:
            """Normalize + dedup + store one combo's results; returns jobs found."""
            for o in results:
                raw = dict(o.raw or {})
                raw.update({
                    "search_query": combo["query"],
                    "search_location": combo["location"],
                    "search_country": combo.get("country", ""),
                    "search_language": combo.get("lang", ""),
                    "search_intent": combo.get("intent", ""),
                })
                o.raw = raw
            processed = [normalize(o) for o in results]
            processed = [o for o in processed if o is not None]
            report.opportunities_fetched += len(processed)
            source_report["fetched"] += len(results)
            source_report["normalized"] += len(processed)
            dup = find_duplicates(session, processed)
            seen_canonical = set()
            combo_new = 0
            for idx, opp in enumerate(processed):
                if idx in dup:
                    report.duplicates += 1
                    source_report["duplicates"] += 1
                    continue
                canonical = opp.canonical_job_id()
                if canonical in seen_canonical:  # same vacancy from another source this run
                    report.duplicates += 1
                    source_report["duplicates"] += 1
                    continue
                seen_canonical.add(canonical)
                src_type = opp.effective_source_type()
                qmap = discovery_cfg.get("source_quality") or {}
                quality = int(qmap.get(src_type, opp.effective_quality())) if qmap else opp.effective_quality()
                careers_url = (opp.raw or {}).get("page", "") if src_type in ("company_career", "ats") else ""
                company = mem.store.get_or_create_company(
                    session, opp.company or "Unknown", opp.url, opp.country,
                    careers_url=careers_url, source=opp.source,
                    sponsorship_signal=opp.sponsorship_signal,
                    international_recruitment_signal=opp.international_candidate_signal,
                )
                job_data = {
                    "source": opp.source,
                    "external_id": opp.external_id,
                    "dedup_key": opp.dedup_key(),
                    "title": opp.title,
                    "company_id": company.id,
                    "location": opp.location,
                    "country": opp.country,
                    "description": opp.description,
                    "url": opp.url,
                    "posted_at": opp.posted_at,
                    "employment_type": opp.employment_type or "full_time",
                    "salary": opp.salary or "",
                    "contact_email": opp.contact_email or "",
                    "status": "new",
                    "source_type": src_type,
                    "source_quality": quality,
                    "source_confidence": quality,
                    "closing_at": opp.closing_at,
                    "language": opp.language or combo.get("lang", ""),
                    "sponsorship_signal": opp.sponsorship_signal,
                    "international_candidate_signal": opp.international_candidate_signal,
                    "relocation_signal": opp.relocation_signal,
                    "work_permit_signal": opp.work_permit_signal,
                    "verification_status": opp.verification_status,
                    "search_query": combo["query"],
                    "search_language": combo.get("lang", ""),
                    "search_country": combo.get("country", opp.country),
                    "canonical_job_id": canonical,
                    "freshness": freshness_label(opp.posted_at),
                    "opportunity_type": classify_opportunity(opp.title, opp.description, src_type),
                    "application_method": "UNKNOWN",
                    "application_url": "",
                }
                job, created = mem.store.upsert_job(session, job_data)
                if created:
                    verification = EmailVerificationService(session).verify_job(job, source_type=src_type)
                    # Never copy an unverified string into a job as a sendable recipient.
                    job.contact_email = verification.email if verification.verified else ""
                    method, application_url = detect_application_method(
                        text=f"{job.title}\n{job.description}", url=job.url, source_type=src_type,
                        has_verified_email=verification.verified)
                    job.application_method, job.application_url = method, application_url
                    mem.store.enqueue_notification(session, "JOB_FOUND", job_id=job.id,
                                                   payload={"opportunity_id": mem.store.opportunity_id(job)})
                    report.new_jobs += 1
                    combo_new += 1
                    source_report["new"] += 1
            mem.store.record_query(session, combo["query"], combo.get("country", ""),
                                   source_name, jobs_found=len(processed),
                                   relevant_jobs=combo_new)
            return len(processed)

        async def search(combo, _connector=connector):
            try:
                return await _connector.search(combo["query"], combo["location"])
            except (TypeError, NotImplementedError):
                return []

        failure = ""
        try:
            if parallel:  # §26 — all searches dispatched at once, writes stay ordered
                report.combinations_attempted += len(combos)
                source_report["queries"] += len(combos)
                batch = await asyncio.gather(*(search(c) for c in combos), return_exceptions=True)
                for combo, results in zip(combos, batch, strict=True):
                    if isinstance(results, BaseException):
                        raise results  # connector-level isolation below
                    items_found += process(combo, results, source_name)
            else:
                for combo in combos:
                    report.combinations_attempted += 1
                    source_report["queries"] += 1
                    items_found += process(combo, await search(combo), source_name)
        except Exception as exc:  # connector-level isolation
            logger.exception("Discovery connector %s failed", source_name)
            report.source_errors.append(f"{source_name}: {exc}")
            mem.store.record_event(session, "discovery", f"connector failed: {exc}",
                                   "error", {"source": source_name})
            failure = f"{source_name}: {exc}"
            source_report["error"] = str(exc)
            if "429" in str(exc) or "rate limit" in str(exc).lower():
                source_report["rate_status"] = "limited"
        finally:
            mem.store.mark_source_fetched(session, source, items_found, error=failure)
            report.source_reports.append(source_report)

    mem.store.record_event(session, "discovery",
                           f"found {report.new_jobs} new jobs ({report.opportunities_fetched} fetched, "
                           f"{report.duplicates} dupes)", "info", {"new": report.new_jobs})
    return report
