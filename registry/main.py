"""Forge artifact registry HTTP API."""

from __future__ import annotations

import hashlib
import json
import os

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from registry.auth import require_auth
from registry.metadata import get_artifact, insert_artifact, is_valid_semver, list_versions
from registry.storage import get_blob, store_blob, verify_checksum

app = FastAPI(title="Forge Registry")

security = HTTPBearer()


def _auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    try:
        return require_auth(f"Bearer {credentials.credentials}")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/artifacts/{name}/{version}", status_code=201)
async def publish_artifact(
    name: str,
    version: str,
    file: UploadFile = File(...),
    checksum: str = Form(...),
    deps: str = Form("[]"),
    publisher: str = Depends(_auth),
):

    if not is_valid_semver(version):
        raise HTTPException(status_code=400, detail=f"Invalid semver version: {version}")

    existing = get_artifact(name, version)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"{name}@{version} already exists (immutable)",
        )

    data = await file.read()

    if not verify_checksum(data, checksum):
        actual = hashlib.sha256(data).hexdigest()
        raise HTTPException(
            status_code=400,
            detail=f"Checksum mismatch: declared={checksum}, actual=sha256:{actual}",
        )

    sha256 = store_blob(data)

    try:
        dep_list = json.loads(deps)
    except json.JSONDecodeError:
        dep_list = []

    try:
        insert_artifact(name, version, sha256, len(data), publisher, dep_list)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"name": name, "version": version, "sha256": sha256, "size": len(data)}


@app.get("/artifacts/{name}/{version}/meta")
def artifact_meta(name: str, version: str):
    meta = get_artifact(name, version)
    if not meta:
        raise HTTPException(status_code=404, detail=f"{name}@{version} not found")
    return meta


@app.get("/artifacts/{name}/{version}")
def download_artifact(name: str, version: str):
    meta = get_artifact(name, version)
    if not meta:
        raise HTTPException(status_code=404, detail=f"{name}@{version} not found")
    data = get_blob(meta["sha256"])
    if not data:
        raise HTTPException(status_code=404, detail="Blob not found")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Artifact-SHA256": meta["sha256"]},
    )


@app.get("/artifacts/{name}")
def list_artifact_versions(name: str):
    versions = list_versions(name)
    return {"name": name, "versions": [v["version"] for v in versions]}


if __name__ == "__main__":
    import uvicorn

    import config

    uvicorn.run(app, host=config.REGISTRY_HOST, port=config.REGISTRY_PORT)
