#!/usr/bin/env python3
"""Validate the deploy configuration files without touching a provider.

Deploy bugs in this repo have twice been configuration bugs, not code bugs
(a worker that could not serve HTTP, a container bound to its own loopback). This
script makes the cheap, mechanical mistakes fail in CI instead of in production:

    python scripts/check_configs.py

Checked:
- render.yaml / deploy/render-starter.yaml parse, are web services, expose
  /healthz, and only ask for a disk when the plan can have one;
- every GitHub workflow parses and declares at least one job;
- docker-compose.yml parses, publishes its status port on host loopback only,
  and sets API_HOST=0.0.0.0 inside the container (the two halves must agree);
- dashboard/vercel.json parses and serves the `public` directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def fail(message: str) -> None:
    FAILURES.append(message)


def load_yaml(relative: str) -> dict[str, Any]:
    path = ROOT / relative
    if not path.exists():
        fail(f"{relative}: missing")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"{relative}: invalid YAML: {exc}")
        return {}
    if not isinstance(data, dict):
        fail(f"{relative}: expected a mapping at the top level")
        return {}
    return data


def check_render(relative: str, *, expect_plan: str, expect_disk: bool) -> None:
    data = load_yaml(relative)
    services = data.get("services")
    if not isinstance(services, list) or not services:
        fail(f"{relative}: expected a non-empty services list")
        return
    service = services[0]
    if service.get("type") != "web":
        fail(
            f"{relative}: type must be 'web' — a worker cannot serve the console "
            "and logs 'No open ports detected'"
        )
    if service.get("plan") != expect_plan:
        fail(f"{relative}: expected plan {expect_plan!r}, got {service.get('plan')!r}")
    if service.get("healthCheckPath") != "/healthz":
        fail(f"{relative}: healthCheckPath must be /healthz")
    if not service.get("startCommand"):
        fail(f"{relative}: startCommand is required")
    has_disk = "disk" in service
    if has_disk != expect_disk:
        fail(
            f"{relative}: disk block {'unexpected' if has_disk else 'missing'} "
            f"for plan {expect_plan!r}"
        )

    env = {item.get("key"): item for item in service.get("envVars", []) if isinstance(item, dict)}
    if env.get("LIVE", {}).get("value") != "false":
        fail(f"{relative}: LIVE must default to 'false' in the blueprint")
    ledger = env.get("LEDGER_PATH", {}).get("value", "")
    if expect_disk and not str(ledger).startswith("/var/data"):
        fail(f"{relative}: LEDGER_PATH should live on the mounted disk (/var/data)")
    if not expect_disk and str(ledger).startswith("/var/data"):
        fail(f"{relative}: LEDGER_PATH points at an unmounted disk on the free plan")


def check_workflows() -> None:
    directory = ROOT / ".github" / "workflows"
    workflows = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    if not workflows:
        fail(".github/workflows: no workflows found")
    for path in workflows:
        data = load_yaml(str(path.relative_to(ROOT)))
        jobs = data.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            fail(f"{path.name}: no jobs defined")


def check_compose() -> None:
    data = load_yaml("docker-compose.yml")
    services = data.get("services")
    if not isinstance(services, dict) or "bot" not in services:
        fail("docker-compose.yml: expected a 'bot' service")
        return
    bot = services["bot"]
    ports = [str(port) for port in bot.get("ports", [])]
    if not any(port.startswith("127.0.0.1:") for port in ports):
        fail("docker-compose.yml: the status port must be published on host loopback only")
    environment = bot.get("environment", {})
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment if "=" in item)
    if str(environment.get("API_HOST")) != "0.0.0.0":
        fail(
            "docker-compose.yml: API_HOST must be 0.0.0.0 inside the container, "
            "otherwise Docker's port publishing cannot reach it"
        )


def check_keepalive_target() -> None:
    """The keep-alive workflow needs a URL from the variable or the committed file."""
    path = ROOT / "deploy" / "bot-url.txt"
    if not path.exists():
        fail("deploy/bot-url.txt: missing (keep-alive has no fallback URL)")
        return
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        fail("deploy/bot-url.txt: no URL line")
        return
    url = lines[0]
    if not url.startswith("https://"):
        fail(f"deploy/bot-url.txt: expected an https:// URL, got {url!r}")
    if url.endswith("/"):
        fail("deploy/bot-url.txt: drop the trailing slash")
    if " " in url:
        fail(f"deploy/bot-url.txt: unexpected whitespace in {url!r}")
    workflow = (ROOT / ".github" / "workflows" / "keepalive.yml").read_text(encoding="utf-8")
    if "deploy/bot-url.txt" not in workflow:
        fail("keepalive.yml: does not fall back to deploy/bot-url.txt")


def check_vercel() -> None:
    path = ROOT / "dashboard" / "vercel.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"dashboard/vercel.json: {exc}")
        return
    if data.get("outputDirectory") != "public":
        fail("dashboard/vercel.json: outputDirectory should be 'public'")
    api_dir = ROOT / "dashboard" / "api"
    if not api_dir.is_dir():
        fail("dashboard/api: serverless functions directory is missing")


def main() -> int:
    check_render("render.yaml", expect_plan="free", expect_disk=False)
    check_render("deploy/render-starter.yaml", expect_plan="starter", expect_disk=True)
    check_workflows()
    check_keepalive_target()
    check_compose()
    check_vercel()

    if FAILURES:
        print(f"{len(FAILURES)} configuration problem(s):")
        for message in FAILURES:
            print(f"  - {message}")
        return 1
    print("Deploy configuration looks consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
