import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

CREDENTIALS_PATH = Path.home() / ".forge" / "credentials"


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise SystemExit("Run 'forge login <url>' first.")

    with CREDENTIALS_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_credentials(url: str, token: str) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"url": url.rstrip("/"), "token": token}

    with CREDENTIALS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")

    os.chmod(CREDENTIALS_PATH, 0o600)


def auth_headers() -> dict:
    credentials = load_credentials()
    return {"Authorization": f"Bearer {credentials['token']}"}


def base_url() -> str:
    return load_credentials()["url"]


def checksum_file(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    return "sha256:" + sha256.hexdigest()


def print_response_error(response: requests.Response) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    print(f"HTTP {response.status_code}: {detail}", file=sys.stderr)


def command_login(args: argparse.Namespace) -> int:
    token = args.token or getpass.getpass("Forge token: ")
    save_credentials(args.url, token)
    print(f"Credentials saved to {CREDENTIALS_PATH}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    pipeline_path = Path(args.pipeline)
    with pipeline_path.open("rb") as pipeline_file:
        response = requests.post(
            f"{base_url()}/runs",
            headers=auth_headers(),
            files={"pipeline": (pipeline_path.name, pipeline_file, "application/x-yaml")},
            timeout=30,
        )

    if response.status_code >= 400:
        print_response_error(response)
        return 1

    run_id = response.json()["run_id"]
    print(run_id)

    if not args.follow:
        return 0

    command_logs(argparse.Namespace(run_id=run_id, follow=True))
    status_response = requests.get(f"{base_url()}/runs/{run_id}", headers=auth_headers(), timeout=30)
    if status_response.status_code >= 400:
        print_response_error(status_response)
        return 1

    status = status_response.json().get("status")
    return 0 if status == "succeeded" else 1


def command_logs(args: argparse.Namespace) -> int:
    response = requests.get(
        f"{base_url()}/runs/{args.run_id}/logs",
        headers=auth_headers(),
        params={"follow": str(args.follow).lower()},
        stream=args.follow,
        timeout=None if args.follow else 30,
    )

    if response.status_code >= 400:
        print_response_error(response)
        return 1

    if args.follow:
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data: "):
                continue
            event = json.loads(raw_line.removeprefix("data: "))
            print(format_log_event(event), flush=True)
    else:
        for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
            if chunk:
                print(chunk, end="")

    return 0


def command_publish(args: argparse.Namespace) -> int:
    artifact_path = Path(args.path)
    checksum = checksum_file(artifact_path)

    with artifact_path.open("rb") as artifact_file:
        response = requests.post(
            f"{base_url()}/artifacts/{args.name}/{args.version}",
            headers=auth_headers(),
            files={"file": (artifact_path.name, artifact_file)},
            data={"checksum": checksum},
            timeout=120,
        )

    if response.status_code >= 400:
        print_response_error(response)
        return 1

    print(f"Published {args.name}@{args.version} ({checksum})")
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    pipeline_path = Path(args.pipeline)
    with pipeline_path.open("rb") as pipeline_file:
        response = requests.post(
            f"{base_url()}/resolve",
            headers=auth_headers(),
            files={"pipeline": (pipeline_path.name, pipeline_file, "application/x-yaml")},
            timeout=30,
        )

    if response.status_code >= 400:
        print_response_error(response)
        return 1

    print(json.dumps(response.json(), indent=2, sort_keys=True))
    return 0


def command_ls(args: argparse.Namespace) -> int:
    response = requests.get(f"{base_url()}/artifacts/{args.package}", headers=auth_headers(), timeout=30)

    if response.status_code >= 400:
        print_response_error(response)
        return 1

    versions = response.json().get("versions", [])
    for version in versions:
        print(version)
    return 0


def format_log_event(event: dict) -> str:
    return f"[{event.get('ts')}] [{event.get('job')}] {event.get('line')}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description="Forge CI/CD command line tool")
    subcommands = parser.add_subparsers(dest="command", required=True)

    login = subcommands.add_parser("login", help="Store Forge URL and token")
    login.add_argument("url")
    login.add_argument("--token", help="Token to store. If omitted, you will be prompted.")
    login.set_defaults(func=command_login)

    run = subcommands.add_parser("run", help="Submit a pipeline")
    run.add_argument("pipeline")
    run.add_argument("--follow", action="store_true", help="Tail logs and exit non-zero if the run fails")
    run.set_defaults(func=command_run)

    logs = subcommands.add_parser("logs", help="Print run logs")
    logs.add_argument("run_id")
    logs.add_argument("--follow", action="store_true", help="Stream logs as Server-Sent Events arrive")
    logs.set_defaults(func=command_logs)

    publish = subcommands.add_parser("publish", help="Upload an artifact")
    publish.add_argument("path")
    publish.add_argument("--name", required=True)
    publish.add_argument("--version", required=True)
    publish.set_defaults(func=command_publish)

    resolve = subcommands.add_parser("resolve", help="Resolve a pipeline without running it")
    resolve.add_argument("pipeline")
    resolve.set_defaults(func=command_resolve)

    list_versions = subcommands.add_parser("ls", help="List package versions")
    list_versions.add_argument("package")
    list_versions.set_defaults(func=command_ls)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
