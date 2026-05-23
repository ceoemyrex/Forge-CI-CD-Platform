# Forge CI/CD Platform

Forge is a small CI/CD platform with an integrated artifact registry. It accepts YAML pipelines, resolves dependencies before builds, runs jobs in isolated Docker containers, streams logs live, publishes artifacts, and sends Slack alerts for important events.

Public URL for grading: `http://YOUR_STATIC_IP_OR_DOMAIN`

Replace the URL above after deploying to the VPS.

## Pipeline YAML

```yaml
name: build-lib-http          # Human-readable pipeline name
version: 1.0.0                # Pipeline version

dependencies:                 # Packages to pull from the registry before jobs run
  - name: lib-core
    version: "^1.0.0"

jobs:
  build:
    runtime: alpine:3.18      # Docker image used for this job
    resources:
      cpu: 1.0
      memory: 512Mi
    steps:
      - name: test
        run: "sh ./test.sh"
      - name: package
        run: "tar czf out.tar.gz src/"

artifacts:
  - name: lib-http
    version: 1.0.0
    path: ./out.tar.gz
```

## HTTP API

Engine endpoints:

- `POST /runs`
- `GET /runs/{id}`
- `GET /runs/{id}/lockfile`
- `GET /runs/{id}/logs`
- `GET /runs/{id}/logs?follow=true`

Registry endpoints expected by the task:

- `POST /artifacts/{name}/{version}`
- `GET /artifacts/{name}/{version}`
- `GET /artifacts/{name}/{version}/meta`
- `GET /artifacts/{name}`

## CLI

Install locally:

```bash
python -m pip install -e .
```

Commands:

```bash
forge login http://localhost:8000
forge run pipeline.yaml
forge run pipeline.yaml --follow
forge logs <run-id>
forge logs <run-id> --follow
forge publish out.tar.gz --name lib-core --version 1.0.0
forge resolve pipeline.yaml
forge ls lib-core
```

`forge login` stores credentials in `~/.forge/credentials` with file mode `0600`.

## DAG Scheduler

The scheduler reads the `jobs` section and uses each job's `needs` list to build a graph. Jobs with no dependencies can run first. Jobs whose dependencies are complete can run in parallel, up to the configured concurrency limit.

If the scheduler sees a cycle, the run must fail before any build starts. If one job fails, jobs that depend on it are marked as skipped.

## Isolation Mechanism

Jobs run inside Docker containers. Each job gets:

- Its own mounted workspace at `/workspace`
- Read-only dependencies at `/workspace/deps`
- CPU and memory limits
- A configured Docker network
- `FORGE_URL` and `FORGE_TOKEN` environment variables

The final grader requirement is stricter than a normal Docker run: outbound network traffic must only reach the registry, and OOM/timeouts must be logged clearly.

## Storage Layer

The registry is expected to store artifact blobs by SHA-256 and store metadata in Postgres or SQLite. The metadata must map `(name, version)` to the blob hash and reject duplicate publishes.

Two pipelines racing to publish the same `(name, version)` should be handled with a database uniqueness constraint. The first insert wins. The second insert gets `409 Conflict`.

## Resolver Determinism

The resolver must read all matching versions from registry metadata, apply semver constraints, and select the highest valid version. The lockfile must be written with stable key ordering so the same pipeline and registry state produce identical JSON bytes every time.

## Log Streaming

Logs are stored as JSON Lines under `storage/logs/<run-id>.jsonl`. Each line contains:

```json
{"ts":"2026-05-23T10:00:00+00:00","job":"build","line":"running tests"}
```

The API streams logs line by line from disk. It does not load the whole file into memory, so a 50MB log can still be read safely. A client connecting mid-build first receives the backlog already written to disk, then receives new lines as they arrive.

## Slack Alerts

Configure Slack in `config.yaml` or with `SLACK_WEBHOOK_URL`.

Alerts use Block Kit and cover:

- Pipeline started
- Pipeline succeeded
- Pipeline failed
- Integrity failure
- Resolution failure

Example alert:

```text
Header: Pipeline failed
Pipeline: build-lib-http
Run ID: abc123
Duration: 42.5s
Failing job: build
```

Add a real screenshot here after sending a test alert in Slack.

## Fresh VPS Setup

1. Point a domain or static IP to the VPS.
2. Install Docker and Docker Compose.
3. Clone this repository.
4. Set the Slack webhook if needed:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

5. Build and start services:

```bash
make up
```

6. Create the first auth token:

```bash
make token
```

7. Login with the CLI:

```bash
forge login http://YOUR_STATIC_IP_OR_DOMAIN
```

8. Submit a pipeline:

```bash
forge run examples/pipeline.yaml --follow
```

## Current Integration Note

Daniel's slice is implemented here: log streaming, CLI, Slack helper, config, compose, and documentation. The registry storage and full dependency resolver are still separate project slices. Placeholder modules are present so the service can import while those pieces are being completed.
