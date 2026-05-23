# config.py

import os
import yaml

# Load from config.yaml
config_path = os.getenv("CONFIG_PATH", "config.yaml")

with open(config_path) as f:
    config_data = yaml.safe_load(f)

engine_config = config_data.get("engine", {})
registry_config = config_data.get("registry", {})
slack_config = config_data.get("slack", {})

# Extract values. The flat keys keep backward compatibility with the old file.
PUBLIC_URL = os.getenv("PUBLIC_URL", config_data.get("public_url", "http://localhost:8000"))
REGISTRY_URL = os.getenv(
    "REGISTRY_URL",
    registry_config.get("url", config_data.get("registry_url", "http://registry:5000")),
)
STORAGE_ROOT = os.getenv(
    "STORAGE_ROOT",
    config_data.get("storage_root", registry_config.get("storage_root", "./storage")),
)
MAX_JOB_DURATION_SEC = int(engine_config.get("max_job_duration_sec", config_data.get("max_job_duration_sec", 1800)))
CONCURRENCY_LIMIT = int(engine_config.get("concurrency_limit", config_data.get("concurrency_limit", 4)))
DEFAULT_CPU = float(engine_config.get("default_cpu", 1.0))
DEFAULT_MEMORY = engine_config.get("default_memory", "512Mi")
SLACK_WEBHOOK_URL = os.getenv(
    "SLACK_WEBHOOK_URL",
    slack_config.get("webhook_url", config_data.get("slack_webhook_url", "")),
)
SLACK_NOTIFY_TAGS = slack_config.get("notify_tags", "")
DB_URL = os.getenv("DB_URL", registry_config.get("db_url", config_data.get("db_url", "sqlite:///forge.db")))

# Ensure storage root exists
os.makedirs(STORAGE_ROOT, exist_ok=True)

print(f"Config loaded: REGISTRY_URL={REGISTRY_URL}, STORAGE_ROOT={STORAGE_ROOT}")
