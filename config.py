"""Load config.yaml and expose settings as module attributes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

_CONFIG: Dict[str, Any] = {}


def _load() -> Dict[str, Any]:
    global _CONFIG
    if _CONFIG:
        return _CONFIG

    path = os.environ.get("FORGE_CONFIG", "config.yaml")
    cfg_path = Path(path)
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            _CONFIG = yaml.safe_load(f) or {}
    else:
        _CONFIG = {}

    return _CONFIG


def get(section: str, key: str, default: Any = None) -> Any:
    cfg = _load()
    return cfg.get(section, {}).get(key, default)


# Engine
ENGINE_HOST = os.environ.get("ENGINE_HOST", get("engine", "host", "0.0.0.0"))
ENGINE_PORT = int(os.environ.get("ENGINE_PORT", get("engine", "port", 8000)))
CONCURRENCY_LIMIT = int(
    os.environ.get("MAX_CONCURRENT_JOBS", get("engine", "max_concurrent_jobs", 4))
)
MAX_JOB_DURATION_SEC = int(
    os.environ.get("JOB_TIMEOUT", get("engine", "default_job_timeout", 1800))
)
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", get("engine", "workspace_base", "/data"))
LOG_DIR = os.environ.get("LOG_DIR", get("engine", "log_dir", "/data/logs"))

# Registry
REGISTRY_HOST = os.environ.get("REGISTRY_HOST", get("registry", "host", "0.0.0.0"))
REGISTRY_PORT = int(os.environ.get("REGISTRY_PORT", get("registry", "port", 8001)))
REGISTRY_URL = os.environ.get(
    "REGISTRY_URL",
    get("registry", "url", f"http://registry:{REGISTRY_PORT}"),
)
REGISTRY_PUBLIC_URL = os.environ.get(
    "REGISTRY_PUBLIC_URL",
    get("registry", "public_url", f"http://localhost:{REGISTRY_PORT}"),
)
ARTIFACT_STORAGE_DIR = os.environ.get(
    "ARTIFACT_STORAGE_DIR", get("registry", "storage_dir", "/data/artifacts")
)
REGISTRY_DB_PATH = os.environ.get(
    "REGISTRY_DB_PATH", get("registry", "db_path", "/data/db/registry.db")
)

# Auth
TOKEN_DB_PATH = os.environ.get(
    "TOKEN_DB_PATH", get("auth", "token_db_path", "/data/db/tokens.db")
)

# Slack
SLACK_WEBHOOK_URL = os.environ.get(
    "SLACK_WEBHOOK_URL", get("slack", "webhook_url", "")
)
SLACK_NOTIFY_USERS = get("slack", "notify_users", ["@devops-team"])

# Docker
JOB_NETWORK = os.environ.get("JOB_NETWORK", get("docker", "job_network", "forge_jobs"))
