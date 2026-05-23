#!/usr/bin/env python3
"""Forge CLI — submit pipelines, stream logs, publish artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import click
import requests
import yaml

CREDENTIALS_PATH = Path.home() / ".forge" / "credentials"


def _load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        click.echo("Not logged in. Run: forge login <url>", err=True)
        sys.exit(1)
    with CREDENTIALS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_credentials(url: str, token: str) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CREDENTIALS_PATH.open("w", encoding="utf-8") as f:
        json.dump({"url": url.rstrip("/"), "token": token}, f)
    os.chmod(CREDENTIALS_PATH, 0o600)


def _headers(creds: dict) -> dict:
    return {"Authorization": f"Bearer {creds['token']}"}


@click.group()
def cli():
    """Forge CI/CD platform CLI."""


@cli.command()
@click.argument("url")
@click.option("--token", prompt=True, hide_input=True, help="Bearer token")
def login(url: str, token: str):
    """Store credentials for the Forge engine."""
    _save_credentials(url, token)
    click.echo(f"Logged in to {url.rstrip('/')}")


@cli.command()
@click.argument("pipeline_file", type=click.Path(exists=True))
@click.option("--follow", is_flag=True, help="Tail logs after submission")
def run(pipeline_file: str, follow: bool):
    """Submit a pipeline YAML file."""
    creds = _load_credentials()
    with open(pipeline_file, "rb") as f:
        resp = requests.post(
            f"{creds['url']}/runs",
            headers=_headers(creds),
            files={"pipeline": (os.path.basename(pipeline_file), f, "application/x-yaml")},
            timeout=60,
        )
    if resp.status_code != 200:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)

    run_id = resp.json()["run_id"]
    click.echo(run_id)

    if follow:
        ctx = click.get_current_context()
        ctx.invoke(logs, run_id=run_id, follow=True)
        status_resp = requests.get(f"{creds['url']}/runs/{run_id}", timeout=30)
        status = status_resp.json().get("status", "unknown")
        if status not in ("succeeded",):
            sys.exit(1)


@cli.command()
@click.argument("run_id")
@click.option("--follow", is_flag=True, help="Stream logs live (SSE)")
def logs(run_id: str, follow: bool):
    """Fetch or stream logs for a run."""
    creds = _load_credentials()
    url = f"{creds['url']}/runs/{run_id}/logs"
    params = {"follow": "true"} if follow else {}

    if follow:
        with requests.get(url, headers=_headers(creds), params=params, stream=True, timeout=None) as resp:
            if resp.status_code != 200:
                click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
                sys.exit(1)
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                click.echo(f"[{event.get('ts', '')}] [{event.get('job', '')}] {event.get('line', '')}")
    else:
        resp = requests.get(url, headers=_headers(creds), params=params, timeout=120)
        if resp.status_code != 200:
            click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
            sys.exit(1)
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            click.echo(f"[{event.get('ts', '')}] [{event.get('job', '')}] {event.get('line', '')}")


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--name", required=True)
@click.option("--version", required=True)
@click.option("--deps", default="[]", help="JSON array of declared dependencies")
def publish(path: str, name: str, version: str, deps: str):
    """Publish an artifact to the registry."""
    creds = _load_credentials()
    registry_url = creds.get("registry_url") or creds["url"].replace(":8000", ":8001")

    with open(path, "rb") as f:
        data = f.read()
    sha256 = hashlib.sha256(data).hexdigest()

    resp = requests.post(
        f"{registry_url}/artifacts/{name}/{version}",
        headers=_headers(creds),
        files={"file": (os.path.basename(path), data)},
        data={"checksum": f"sha256:{sha256}", "deps": deps},
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    click.echo(json.dumps(resp.json(), indent=2))


@cli.command()
@click.argument("pipeline_file", type=click.Path(exists=True))
def resolve(pipeline_file: str):
    """Print the lockfile without running the pipeline."""
    creds = _load_credentials()
    registry_url = creds.get("registry_url") or creds["url"].replace(":8000", ":8001")

    from engine.parser import PipelineParser
    from registry.resolver import DependencyResolver, serialize_lockfile

    with open(pipeline_file, "r", encoding="utf-8") as f:
        pipeline_yaml = f.read()

    parser = PipelineParser()
    config_dict = parser.parse_and_validate(pipeline_yaml)
    resolver = DependencyResolver(registry_url=registry_url)
    lockfile = resolver.resolve(config_dict)
    click.echo(serialize_lockfile(lockfile).rstrip("\n"))


@cli.command("ls")
@click.argument("package")
def list_pkg(package: str):
    """List all versions of a package."""
    creds = _load_credentials()
    registry_url = creds.get("registry_url") or creds["url"].replace(":8000", ":8001")

    resp = requests.get(f"{registry_url}/artifacts/{package}", timeout=30)
    if resp.status_code == 404:
        click.echo("No versions found.")
        return
    if resp.status_code != 200:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)

    data = resp.json()
    for version in data.get("versions", []):
        click.echo(version)


@click.group("admin")
def admin_group():
    """Administrative commands (run directly on the host)."""


@admin_group.command("create-token")
@click.argument("name")
def create_token_cmd(name: str):
    """Create a new bearer token and print it (run on the host, not through the API)."""
    import os, sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from registry.auth import create_token

    token = create_token(name)
    click.echo(f"FORGE_TOKEN={token}")
    click.echo(f"Token identity: {name}", err=True)


cli.add_command(admin_group)


if __name__ == "__main__":
    cli()
