Public URL
Engine API: http://YOUR_EC2_IP:8000
Registry API: http://YOUR_EC2_IP:8001

Pipeline YAML Schema
name: build-lib-http          # required: pipeline name
version: 1.0.0                # required: semver pipeline version
dependencies:
  - name: lib-core
    version: "^1.0.0"

jobs:
  build:
    runtime: alpine:3.18
    resources:
      cpu: 1.0
      memory: 512Mi
    needs: []
    steps:
      - name: test
        run: "sh ./test.sh"
      - name: package
        run: "tar czf out.tar.gz src/"

artifacts:
  - name: lib-http
    version: 1.0.0
    path: ./out.tar.gz

Architecture

DAG Scheduler:
Jobs declare dependencies and execute via topological sorting.

Isolation:
Each job runs in Docker with network and resource isolation.

Storage Layer:
Content-addressable blobs stored in SQLite-backed registry.

Dependency Resolver:
Supports semver (^ ~ exact ranges), selects highest valid version.

Log Streaming:
SSE streaming from append-only logs.

Concurrent Publish Safety:
SQLite UNIQUE constraint prevents duplicate publishes.

Fresh VPS Setup

sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin python3-pip
sudo usermod -aG docker $USER && newgrp docker

cd ~/forge
mkdir -p data/{artifacts,db,logs}
docker compose build
docker compose up -d

docker compose exec registry python3 -c "
import sys, os
os.environ['TOKEN_DB_PATH']='/data/db/tokens.db'
sys.path.insert(0,'/app')
from registry.auth import create_token
print('FORGE_TOKEN=' + create_token('admin'))
"

export FORGE_TOKEN=<token>
echo "FORGE_TOKEN=$FORGE_TOKEN" > .env

docker compose down && docker compose up -d

pip3 install click requests pyyaml --break-system-packages
sudo ln -sf ~/forge/cli/forge.py /usr/local/bin/forge
forge login http://YOUR_IP:8000 --token $FORGE_TOKEN

PHASE 12: Final Verification Checklist

docker compose ps
curl http://localhost:8001/artifacts/nonexistent
curl http://localhost:8000/runs/nonexistent
curl ifconfig.me
docker compose logs -f
