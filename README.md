# Forge — CI/CD Platform with Integrated Artifact Registry

## Public URL

Replace with your VPS address after deployment:

- **Engine API:** `http://YOUR_VPS_IP:8000`
- **Registry API:** `http://YOUR_VPS_IP:8001`

## Pipeline YAML Schema

```yaml
name: build-lib-http          # required: pipeline name
version: 1.0.0                # required: semver pipeline version

dependencies:                 # optional: pulled to ./deps/<name>/ before jobs run
  - name: lib-core
    version: "^1.0.0"         # exact, ^, ~, or comparator range (>=1.0.0 <2.0.0)

jobs:
  build:                      # job name (becomes DAG node)
    runtime: alpine:3.18      # required: Docker image
    resources:
      cpu: 1.0                # CPU cores
      memory: 512Mi           # memory limit (Ki/Mi/Gi)
    needs: []                 # optional: job names this depends on
    steps:
      - name: test
        run: "sh ./test.sh"   # shell command in shared workspace
      - name: package
        run: "tar czf out.tar.gz src/"

artifacts:                    # auto-published after all jobs succeed
  - name: lib-http
    version: 1.0.0            # must be valid semver
    path: ./out.tar.gz        # relative path under workspace
```

See `examples/` for the three required capability pipelines.

## Architecture

### DAG Scheduler

Jobs declare `needs: [other-job]`. The scheduler builds a directed acyclic graph, detects cycles via DFS over `needs` edges before any job runs, topologically sorts with Kahn's algorithm, and groups jobs into parallel execution levels. Independent jobs in the same level run concurrently up to `max_concurrent_jobs` (configurable in `config.yaml`). When a job fails, all transitive dependents are marked `skipped` (not `failed`).

### Isolation Mechanism

Each job runs in a dedicated Docker container on the internal `forge_jobs` network (`internal: true` — no public internet egress). Enforcement:

| Constraint | Implementation |
|---|---|
| Filesystem | Bind-mount only `/workspace` (rw) and `/workspace/deps` (ro); `read_only` rootfs + `/tmp` tmpfs |
| Process | Default Docker PID namespace; `pids_limit=256`; `cap_drop=ALL`; `no-new-privileges` |
| Network | Internal bridge network — containers reach only other services on `forge_jobs` (the registry) |
| CPU/memory | Docker cgroups via `cpu_quota` and `mem_limit` from YAML |
| Time | Per-job wall-clock timeout (default 30 min, configurable) |

OOM kills are detected via container `OOMKilled` state and logged clearly.

### Storage Layer

Content-addressable blob storage: files stored at `data/artifacts/blobs/<sha256[:2]>/<sha256>`. The `(name, version)` coordinate maps to a SHA-256 hash in SQLite. Server-side checksum verification on upload; client-declared checksum mismatch → 400. `UNIQUE(name, version)` constraint enforces immutability → 409 on duplicate.

### Dependency Resolver

Hand-rolled semver parser supporting exact, caret (`^`), tilde (`~`), and comparator ranges. Walks transitive dependencies via registry metadata, detects cycles and version conflicts with clear error messages, and selects the **highest** version satisfying all constraints.

**Determinism:** root dependencies processed in alphabetical order; packages resolved in alphabetical order; lockfile serialized with `sort_keys=True` and compact separators. Same pipeline + same registry state always produces an identical lockfile byte-for-byte.

### Log Streaming

Each log line is written as a JSON event `{"ts","job","line"}` to `data/logs/<run_id>/<job>.log` and appended to `data/logs/<run_id>/combined.jsonl`. The SSE endpoint at `GET /runs/{id}/logs?follow=true` reads `combined.jsonl` incrementally — backlog first, then tail — without loading the full file into memory. A 50MB log remains streamable via offset-based reads.

## Concurrent Publish Safety

Two pipelines racing to publish the same `(name, version)` both attempt an SQLite `INSERT`. The `UNIQUE(name, version)` constraint ensures exactly one succeeds (201) and the other gets 409. SQLite write serialization prevents silent corruption.

## Fresh VPS Setup

```bash
# 1. Install Docker
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin python3-pip git
sudo usermod -aG docker $USER && newgrp docker

# 2. Clone and start
git clone <repo-url> ~/forge && cd ~/forge
docker compose build
docker compose up -d

# 3. Install CLI (on the host)
pip3 install -e . --break-system-packages

# 4. Create first auth token
docker compose exec registry forge admin create-token admin
# Alternatively (without CLI in container):
docker compose exec registry python scripts/create_token.py admin
# Either prints: FORGE_TOKEN=<hex-token>

# 5. Login and run
forge login http://YOUR_VPS_IP:8000 --token <token>
forge run examples/build-lib-core.yaml --follow
forge ls lib-core
```

## CLI Commands

| Command | Description |
|---|---|
| `forge login <url> --token <token>` | Store credentials in `~/.forge/credentials` |
| `forge run <pipeline.yaml> [--follow]` | Submit pipeline; optionally tail logs |
| `forge logs <run-id> [--follow]` | Fetch or stream logs via SSE |
| `forge publish <path> --name <n> --version <v>` | Upload artifact with checksum |
| `forge resolve <pipeline.yaml>` | Print lockfile without running |
| `forge ls <package>` | List published versions |

## HTTP API

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/runs` | Bearer | Submit pipeline → `{run_id}` |
| GET | `/runs/{id}` | — | `{status, jobs, lockfile_url}` |
| GET | `/runs/{id}/lockfile` | — | Lockfile JSON |
| GET | `/runs/{id}/logs?follow=true` | — | SSE stream `{ts, job, line}` |
| POST | `/artifacts/{name}/{version}` | Bearer | Upload → 201 / 400 / 409 |
| GET | `/artifacts/{name}/{version}` | — | Blob download + `X-Artifact-SHA256` |
| GET | `/artifacts/{name}/{version}/meta` | — | Metadata JSON |
| GET | `/artifacts/{name}` | — | `{versions: [...]}` |

**Run statuses:** `queued`, `running`, `succeeded`, `failed`, `integrity_failure`, `conflict_failure`, `cycle_failure`

## Slack Alerts

Configure `slack.webhook_url` in `config.yaml`. Alerts (Block Kit) fire for: pipeline started/succeeded/failed, integrity failures (with `@mentions`), and resolution failures.

## Repository Structure

```
├── engine/         main.py, parser.py, scheduler.py, runner.py, logs.py, slack.py
├── registry/       main.py, storage.py, metadata.py, resolver.py, auth.py
├── cli/forge.py
├── compose.yaml
├── config.yaml
├── config.py
├── examples/       Sample pipeline YAMLs
└── scripts/        create_token.py
```
