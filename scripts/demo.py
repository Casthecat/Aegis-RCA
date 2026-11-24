"""Manage ONLY the dedicated aegis-rca-demo Compose project."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile

from aegis_rca.evaluation.demo_environment import (
    DemoResourceProfile, DemoUpstreamSourceMarker, apply_resource_profile,
    build_prepared_environment,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".cache" / "opentelemetry-demo"
COMPOSE = ROOT / "artifacts" / "demo-compose.json"
PROJECT = "aegis-rca-demo"
FLAGS = ROOT / "artifacts" / "demo-flags"


def validate_image_pins(config):
    for service in config["services"].values():
        image = service.get("image", "")
        if "/open-telemetry/demo:" in image and not image.split(":")[-1].startswith("3.0.0-"):
            raise ValueError("Demo application images must use 3.0.0 tags; regenerate the Compose configuration")


def docker(*args, capture=False, environment=None):
    return subprocess.run(["docker", *args], check=True, text=True,
                          capture_output=capture, encoding="utf-8", env=environment)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare(resource_profile=None):
    marker_path = SOURCE / ".aegis-source.json"
    if not marker_path.exists():
        raise SystemExit("Run scripts/prepare_demo.py first")
    profile = (DemoResourceProfile.model_validate_json(Path(resource_profile).read_bytes())
               if resource_profile is not None else None)
    provenance = COMPOSE.with_name(COMPOSE.stem + ".provenance.json")
    if profile is not None:
        marker_bytes = marker_path.read_bytes()
        DemoUpstreamSourceMarker.model_validate_json(marker_bytes)
        upstream_compose = (SOURCE / "compose.yaml").read_bytes()
        upstream_observability = (SOURCE / "compose.observability.yaml").read_bytes()
    raw = docker("compose", "--project-directory", str(SOURCE),
                 "-f", str(SOURCE / "compose.yaml"),
                 "-f", str(SOURCE / "compose.observability.yaml"),
                 "config", "--format", "json", capture=True,
                 environment={**os.environ, "DEMO_VERSION": "3.0.0", "IMAGE_VERSION": "3.0.0"})
    config = json.loads(raw.stdout)
    validate_image_pins(config)
    if profile is not None:
        config = apply_resource_profile(config, profile)
    config["name"] = PROJECT
    config["networks"]["default"]["name"] = PROJECT
    if not FLAGS.exists():
        shutil.copytree(SOURCE / "src" / "flagd", FLAGS)
    published = {("prometheus", 9090): "19090", ("jaeger", 16686): "16686",
                 ("opensearch", 9200): "19200", ("frontend-proxy", 8080): "18080"}
    for name, service in config["services"].items():
        service.pop("container_name", None)
        service.pop("build", None)
        service["restart"] = "no"
        if name in {"flagd", "flagd-ui"}:
            for volume in service.get("volumes", []):
                if volume.get("target") in {"/etc/flagd", "/app/data"}:
                    volume["source"] = str(FLAGS)
        for port in service.get("ports", []):
            port["host_ip"] = "127.0.0.1"
            key = (name, int(port["target"]))
            if key in published:
                port["published"] = published[key]
            else:
                port.pop("published", None)
    compose_bytes = json.dumps(config, indent=2).encode("utf-8")
    receipt = (build_prepared_environment(profile, compose_bytes, marker_bytes,
               upstream_compose, upstream_observability, raw.stdout.encode("utf-8"))
               if profile is not None else None)
    # Invalidate first and publish the receipt last. Interrupted preparation can
    # never leave new Compose bytes authorized by stale profile provenance.
    provenance.unlink(missing_ok=True)
    _atomic_write(COMPOSE, compose_bytes)
    if receipt is not None:
        _atomic_write(provenance, receipt.model_dump_json(indent=2).encode("utf-8"))
    print(f"Prepared local-only Compose configuration: {COMPOSE}")
    if receipt is not None:
        print(f"Resource profile: {receipt.demo_resource_profile_id}; provenance: {provenance}")
    else:
        print("No resource profile applied; this environment is not bound to a formal pilot CapturePolicy.")
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "up", "status", "stop", "logs"])
    parser.add_argument("--resource-profile", type=Path,
                        help="Explicit frozen DemoResourceProfile JSON; prepare only")
    args = parser.parse_args()
    if args.resource_profile is not None and args.action != "prepare":
        parser.error("--resource-profile is only valid with prepare")
    if args.action == "prepare":
        prepare(args.resource_profile)
        return
    if not COMPOSE.exists():
        raise SystemExit("Run scripts/demo.py prepare first")
    if args.action == "up":
        validate_image_pins(json.loads(COMPOSE.read_text(encoding="utf-8")))
    base = ("compose", "-p", PROJECT, "-f", str(COMPOSE))
    commands = {"up": ("up", "-d", "--no-build", "--wait", "--wait-timeout", "180"),
                "status": ("ps", "--all"), "stop": ("stop",), "logs": ("logs", "--tail", "30")}
    docker(*base, *commands[args.action])


if __name__ == "__main__":
    main()
