from fastapi import FastAPI, HTTPException

app = FastAPI(title="Forge Artifact Registry")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/artifacts/{name}")
async def list_versions(name: str):
    raise HTTPException(status_code=501, detail="Registry storage is owned by the registry task and is not implemented yet")
