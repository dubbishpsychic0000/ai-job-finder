#!/usr/bin/env python
"""Validate and setup credentials from .env for ai-job-finder."""
import os
from pathlib import Path

env_file = Path.cwd() / ".env"
required_keys = {
    "TAVILY_API_KEY": "Web search connector",
    "EMAIL_FROM": "Your email address for applications",
    "ENABLE_EMAIL": "Must be 'true' to enable email",
}

print("🔍 Checking credentials in .env...\n")

missing = []
configured = []

for key, description in required_keys.items():
    value = os.getenv(key, "").strip()
    if value and value not in ("true", "false", ""):
        configured.append(f"✅ {key}: {description}")
    else:
        missing.append(f"❌ {key}: {description}")

if configured:
    print("Configured:")
    for item in configured:
        print(f"  {item}")

if missing:
    print("\nMissing (needed for live pipeline):")
    for item in missing:
        print(f"  {item}")
    print("\n📝 Please add these to .env from your ai-job-finder-vault:")
    print("  nano .env")
else:
    print("\n✅ All required credentials are configured!")
    print("\n🚀 Ready to run: python -m app.cli run-once --real")
