# config.py

import os
import yaml

# Load from config.yaml
config_path = os.getenv("CONFIG_PATH", "config.yaml")

with open(config_path) as f:
    config_data = yaml.safe_load(f)

# Extract values
REGISTRY_URL = os.getenv("REGISTRY_URL", config_data.get("registry_url", "http://registry:5000"))
STORAGE_ROOT = os.getenv("STORAGE_ROOT", config_data.get("storage_root", "./storage"))
MAX_JOB_DURATION_SEC = int(config_data.get("max_job_duration_sec", 1800))
CONCURRENCY_LIMIT = int(config_data.get("concurrency_limit", 4))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", config_data.get("slack_webhook_url", ""))
DB_URL = os.getenv("DB_URL", config_data.get("db_url", "sqlite:///forge.db"))

# Ensure storage root exists
os.makedirs(STORAGE_ROOT, exist_ok=True)

print(f"Config loaded: REGISTRY_URL={REGISTRY_URL}, STORAGE_ROOT={STORAGE_ROOT}")