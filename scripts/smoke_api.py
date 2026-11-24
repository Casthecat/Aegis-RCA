"""Exercise a running localhost API over HTTP, using an explicit synthetic case."""
import json
from pathlib import Path
import time

import httpx

from aegis_rca.fixtures import demo_incident


def main():
    with httpx.Client(base_url="http://127.0.0.1:18001", timeout=3, trust_env=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        response = client.post("/incidents", json={"incident": demo_incident().model_dump(mode="json")})
        assert response.status_code == 202
        id = response.json()["id"]
        for _ in range(100):
            status = client.get(f"/incidents/{id}").json()
            if status["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert status["status"] == "completed"
        report_response = client.get(f"/incidents/{id}/report")
        report_response.raise_for_status()
        report = report_response.json()
        assert report["status"] == "SUPPORTED"
        assert report["mode"] == "synthetic_fixture"
        assert report["underlying_cause"] is None
        output = Path(__file__).resolve().parents[1] / "artifacts" / "api-smoke.json"
        output.write_text(json.dumps({"health": health.json(), "job": status, "report": report}, indent=2), encoding="utf-8")
        print(f"HTTP smoke passed: 202 submission -> completed -> 200 report; {report['tool_calls']} calls")


if __name__ == "__main__":
    main()
