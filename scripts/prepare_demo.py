"""Download a pinned official Demo archive into the ignored project cache."""
import io
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TAG = "3.0.0"


def main():
    target = ROOT / ".cache" / "opentelemetry-demo"
    marker = target / ".aegis-source.json"
    if marker.exists():
        print(marker.read_text(encoding="utf-8"))
        return
    if target.exists():
        raise SystemExit("Target exists without source marker; inspect it before retrying.")
    url = f"https://codeload.github.com/open-telemetry/opentelemetry-demo/zip/refs/tags/{TAG}"
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(100_000_001)
    if len(archive) > 100_000_000:
        raise SystemExit("Archive exceeds 100 MB limit")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        items = []
        total = 0
        for item in bundle.infolist():
            parts = Path(item.filename).parts[1:]
            destination = target.joinpath(*parts).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise SystemExit("Archive path escapes target")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise SystemExit("Archive symlinks are not accepted")
            total += item.file_size
            if total > 250_000_000:
                raise SystemExit("Expanded archive exceeds limit")
            items.append((item, destination))
        for item, destination in items:
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(bundle.read(item))
    metadata = {"repository": "https://github.com/open-telemetry/opentelemetry-demo",
                "tag": TAG, "archive_url": url}
    marker.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Prepared {target}")


if __name__ == "__main__":
    main()
