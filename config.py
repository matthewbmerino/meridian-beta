"""Shared config — resolves .env from dev (project root) or prod (/etc/meridian/env)."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("meridian.config")

_PROJECT_ROOT = Path(__file__).resolve().parent
_PROD_ENV = Path("/etc/meridian/env")

# Dev: .env in the project root. Prod: /etc/meridian/env (root-only), which
# overrides dev values when both exist (systemd runs with cwd = project root,
# so both files are reachable — prod wins). The existence check must not raise
# when /etc/meridian is root-only and we're not root.
try:
    if _PROD_ENV.exists():
        load_dotenv(_PROD_ENV, override=True)
except OSError:
    pass
load_dotenv(_PROJECT_ROOT / ".env")

# A packaged install keeps its secrets outside the app tree so an update can
# replace the code without touching them. This file, when set, wins.
_ENV_OVERRIDE = os.getenv("MERIDIAN_ENV_FILE")
if _ENV_OVERRIDE:
    try:
        load_dotenv(_ENV_OVERRIDE, override=True)
    except OSError:
        pass

FRED_API_KEY = os.getenv("FRED_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY")
# Schwab Trader API — the equity-feed swap-in (data/schwab.py). Deliberately
# NOT in _REQUIRED_KEYS: the app runs on Massive today, and the module refuses
# loudly when these are unset.
SCHWAB_APP_KEY = os.getenv("SCHWAB_APP_KEY")
SCHWAB_APP_SECRET = os.getenv("SCHWAB_APP_SECRET")
SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")

# Fail fast on missing keys that would otherwise surface as confusing
# downstream errors ("OLLAMA_API_KEY is not set" from compute.py).
_REQUIRED_KEYS = ["MASSIVE_API_KEY", "PERPLEXITY_API_KEY", "OLLAMA_API_KEY"]
missing = [k for k in _REQUIRED_KEYS if not os.getenv(k)]
if missing:
    log.warning("Missing required API keys in environment: %s", ", ".join(missing))
