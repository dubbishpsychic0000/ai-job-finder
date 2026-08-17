"""Gemini demo runner: init + full pipeline, dump JSON result to a file.

Run in the background and poll data/gemini_result.json.
Sync Gemini calls are slow, so keep per-job work minimal here — we just log.
"""
from __future__ import annotations

import json
import sys
import time

from app.database import init_db
from app.workflows.pipeline import run_pipeline

OUT = r"C:\Users\hp\Downloads\bak sa7bi\data\gemini_result.json"

start = time.perf_counter()
print("[runner] init db", flush=True)
init_db()

print("[runner] running pipeline", flush=True)
result = run_pipeline()
data = {
    "elapsed_seconds": round(time.perf_counter() - start, 1),
    "discovery": result.discovery,
    "analysis": result.analysis,
    "action": {
        "applied": result.action.get("applied"),
        "asked": result.action.get("asked"),
        "investigated": result.action.get("investigated", 0),
        "blocked": len(result.action.get("blocked", [])),
    },
    "followup": result.followup,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print(f"[runner] done in {data['elapsed_seconds']}s -> {OUT}", flush=True)