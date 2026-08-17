"""Local fixture/static source (custom_source in the spec).

Reads opportunities from JSON files on disk. Used for demos, offline work and
golden-set tests, and as an example of how to add a bespoke source later.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from app.connectors.base import Opportunity, infer_country, parse_date

DEFAULT_GLOB = "*.json"


class StaticFilesSource:
    name = "static_files"
    kind = "static"
    source_type = "job_board"
    access_mode = "user_provided"
    policy_notice = "Local fixture/offline source; only reads user-provided files."

    def __init__(self, path: str | Path, live: bool = False):
        self.path = Path(path)
        self.live = live  # repeatably include all items (tests) vs realistic sampling

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        items = self._load_all()
        seed = f"{query}|{location}"
        rng = random.Random(seed)
        pool = items
        if not self.live:
            pool = rng.sample(items, k=min(8, len(items))) if items else []
        for it in pool:
            text = f"{it.get('title', '')} {it.get('description', '')} {it.get('location', '')}".lower()
            words = [w for w in query.lower().split() if len(w) > 3]
            if words and not any(w in text for w in words):
                continue
            out.append(Opportunity(
                source="static_files",
                external_id=str(it.get("external_id", it.get("url", ""))),
                title=it.get("title", ""),
                company=it.get("company", ""),
                location=it.get("location", ""),
                country=it.get("country") or infer_country(it.get("location", "")),
                description=it.get("description", ""),
                url=it.get("url", ""),
                posted_at=parse_date(it.get("posted_at")),
                employment_type=it.get("employment_type", "full_time"),
                salary=it.get("salary"),
                contact_email=it.get("contact_email"),
                raw={"file": str(it.get("_file"))},
            ))
        return out

    def _load_all(self) -> list[dict]:
        out: list[dict] = []
        for f in sorted(self.path.glob("*.json")):
            with f.open("r", encoding="utf-8") as fh:
                for item in json.load(fh):
                    item["_file"] = f.name
                    out.append(item)
        return out
