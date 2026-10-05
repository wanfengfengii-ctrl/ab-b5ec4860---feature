"""One-shot verification service.

Steps (each prints a clear banner; the process exit code reports the result):
  1. run the unit/integration test suite (pytest)
  2. application build check (byte-compile + import the ASGI app)
  3. wait for the API to be healthy, then submit a business smoke request
     that contains BOTH a threshold crossing and a coverage gap, and assert
     the adjudication; also assert that shuffled input order and different
     decimal/timestamp representations yield an identical conclusion.

Run with:  python -m verify.run
Environment:
  API_BASE_URL  base URL of the API under test (default http://localhost:8000)
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

import requests

API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")

_failures: list = []


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        _failures.append(name)


def run_cmd(cmd: list) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def smoke_payload() -> dict:
    """Crossing (6->10 degC across the 8 degC threshold) AND a coverage gap
    (00:25 -> 00:45 is 1200s > 600s max interval)."""
    return {
        "transport_start": "2026-01-01T00:00:00Z",
        "transport_end": "2026-01-01T00:55:00Z",
        "threshold_celsius": 8.0,
        "max_interval_seconds": 600,
        "max_single_excursion_seconds": 900,
        "degree_minute_budget": 30.0,
        "readings": [
            {"time": "2026-01-01T00:00:00Z", "celsius": 6.0},
            {"time": "2026-01-01T00:10:00Z", "celsius": 10.0},
            {"time": "2026-01-01T00:20:00Z", "celsius": 10.0},
            {"time": "2026-01-01T00:25:00Z", "celsius": 6.0},
            {"time": "2026-01-01T00:45:00Z", "celsius": 7.0},
            {"time": "2026-01-01T00:55:00Z", "celsius": 7.0},
        ],
    }


def representation_variant(payload: dict) -> dict:
    """Same business facts, different order and decimal/offset spellings."""
    variant = dict(payload)
    variant["threshold_celsius"] = 8.00
    variant["degree_minute_budget"] = 30.00
    alt = [
        {"time": "2026-01-01T00:00:00.000Z", "celsius": 6.00},
        {"time": "2026-01-01T02:10:00+02:00", "celsius": 10.000},  # == 00:10Z
        {"time": "2026-01-01T00:20:00Z", "celsius": 10.0},
        {"time": "2026-01-01T00:25:00.000000Z", "celsius": 6},
        {"time": "2026-01-01T00:45:00Z", "celsius": 7.0},
        {"time": "2026-01-01T00:55:00Z", "celsius": 7.00},
    ]
    random.Random(20260101).shuffle(alt)
    variant["readings"] = alt
    return variant


def phased_payload() -> dict:
    """Same trajectory as smoke_payload() but with per-phase limits:

    phase 1 (loading) limit 8 degC until 00:20, phase 2 (steady transport)
    limit 9 degC from 00:20.  The request therefore contains a threshold
    switch, crossings solved against both limits, and the same coverage gap.
    """
    payload = smoke_payload()
    del payload["threshold_celsius"]
    payload["threshold_periods"] = [
        {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:20:00Z",
            "threshold_celsius": 8.0,
        },
        {
            "start": "2026-01-01T00:20:00Z",
            "end": "2026-01-01T00:55:00Z",
            "threshold_celsius": 9.0,
        },
    ]
    return payload


def wait_for_api(timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(API_BASE + "/healthz", timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def main() -> int:
    # 1. code tests ---------------------------------------------------------
    step("1/3 unit & integration tests (pytest)")
    check("pytest", run_cmd([sys.executable, "-m", "pytest", "-q", "tests"]) == 0)

    # 2. application build --------------------------------------------------
    step("2/3 application build check")
    check(
        "byte-compile",
        run_cmd([sys.executable, "-m", "compileall", "-q", "app", "verify"]) == 0,
    )
    check(
        "import app.main",
        run_cmd([sys.executable, "-c", "import app.main; print('import ok:', app.main.app.title)"]) == 0,
    )

    # 3. business smoke request ---------------------------------------------
    step("3/3 business smoke request (threshold crossing + coverage gap)")
    check("api healthy", wait_for_api(), f"no /healthz 200 from {API_BASE}")

    if not _failures:
        try:
            payload = smoke_payload()
            r = requests.post(API_BASE + "/api/cold-chain/exposure", json=payload, timeout=10)
            check("smoke status 200", r.status_code == 200, f"got {r.status_code}: {r.text[:400]}")
            body = r.json()

            check("verdict is fail", body.get("verdict") == "fail", json.dumps(body)[:400])
            codes = {reason["code"] for reason in body.get("reasons", [])}
            check(
                "reasons cover gap + single-excursion",
                {"coverage_gap", "single_excursion_exceeded"} <= codes,
                f"codes={codes}",
            )
            check(
                "one excursion 00:05:00-00:22:30, 1050s, 27.5 deg-min",
                body.get("excursions")
                == [
                    {
                        "start": "2026-01-01T00:05:00Z",
                        "end": "2026-01-01T00:22:30Z",
                        "duration_seconds": "1050",
                        "degree_minutes": "27.5",
                    }
                ],
                json.dumps(body.get("excursions")),
            )
            check(
                "total degree-minutes 27.5",
                body.get("total_degree_minutes") == "27.5",
                str(body.get("total_degree_minutes")),
            )
            check(
                "one coverage gap 00:25-00:45 (1200s)",
                body.get("coverage_gaps")
                == [
                    {
                        "start": "2026-01-01T00:25:00Z",
                        "end": "2026-01-01T00:45:00Z",
                        "duration_seconds": "1200",
                    }
                ],
                json.dumps(body.get("coverage_gaps")),
            )

            # determinism: order + decimal representation must not matter
            r2 = requests.post(
                API_BASE + "/api/cold-chain/exposure",
                json=representation_variant(payload),
                timeout=10,
            )
            check(
                "order/representation invariant",
                r2.status_code == 200 and r2.json() == body,
                f"status={r2.status_code} body={r2.text[:400]}",
            )

            # locatable 422 on duplicate timestamps
            bad = smoke_payload()
            bad["readings"][1]["time"] = "2026-01-01T00:00:00.000Z"
            r3 = requests.post(API_BASE + "/api/cold-chain/exposure", json=bad, timeout=10)
            loc_ok = (
                r3.status_code == 422
                and r3.json()["detail"][0]["loc"] == ["body", "readings", 1, "time"]
            )
            check("duplicate timestamp -> locatable 422", loc_ok, f"got {r3.status_code}: {r3.text[:300]}")

            # per-phase limits: one request containing a threshold switch
            # (8 -> 9 degC at 00:20), crossings under both limits and the
            # coverage gap; pieces merge at the switch because both sides are
            # strictly above their phase limit (300 + 1200 + 37.5 deg-sec).
            rp = requests.post(
                API_BASE + "/api/cold-chain/exposure", json=phased_payload(), timeout=10
            )
            check("phased status 200", rp.status_code == 200, f"got {rp.status_code}: {rp.text[:400]}")
            pbody = rp.json()
            pcodes = {reason["code"] for reason in pbody.get("reasons", [])}
            check(
                "phased reasons cover gap + single-excursion",
                {"coverage_gap", "single_excursion_exceeded"} <= pcodes,
                f"codes={pcodes}",
            )
            check(
                "phased excursion 00:05:00-00:21:15, 975s, 25.625 deg-min",
                pbody.get("excursions")
                == [
                    {
                        "start": "2026-01-01T00:05:00Z",
                        "end": "2026-01-01T00:21:15Z",
                        "duration_seconds": "975",
                        "degree_minutes": "25.625",
                    }
                ],
                json.dumps(pbody.get("excursions")),
            )
            check(
                "phased total degree-minutes 25.625",
                pbody.get("total_degree_minutes") == "25.625",
                str(pbody.get("total_degree_minutes")),
            )

            # phase periods given in reverse input order -> identical result
            rev = phased_payload()
            rev["threshold_periods"] = list(reversed(rev["threshold_periods"]))
            rpr = requests.post(API_BASE + "/api/cold-chain/exposure", json=rev, timeout=10)
            check(
                "phased period order invariant",
                rpr.status_code == 200 and rpr.json() == pbody,
                f"status={rpr.status_code} body={rpr.text[:400]}",
            )

            # a phase period that leaves the transport uncovered -> locatable 422
            bad_periods = phased_payload()
            bad_periods["threshold_periods"][1]["start"] = "2026-01-01T00:21:00Z"
            r4 = requests.post(
                API_BASE + "/api/cold-chain/exposure", json=bad_periods, timeout=10
            )
            period_loc_ok = (
                r4.status_code == 422
                and r4.json()["detail"][0]["loc"]
                == ["body", "threshold_periods", 1, "start"]
            )
            check(
                "non-adjacent threshold periods -> locatable 422",
                period_loc_ok,
                f"got {r4.status_code}: {r4.text[:300]}",
            )
        except requests.RequestException as exc:
            check("smoke request executed", False, str(exc))

    step("summary")
    if _failures:
        print(f"VERIFY FAILED: {len(_failures)} check(s) failed: {', '.join(_failures)}", flush=True)
        return 1
    print("VERIFY OK: tests, build and smoke request all passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
