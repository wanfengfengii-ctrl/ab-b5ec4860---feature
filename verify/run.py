"""One-shot verification service.

Steps (each prints a clear banner; the process exit code reports the result):
  1. run the unit/integration test suite (pytest)
  2. application build check (byte-compile + import the ASGI app)
  3. wait for the API to be healthy, then submit a business smoke request
     that contains BOTH a threshold crossing and a coverage gap, and assert
     the adjudication; also assert that shuffled input order and different
     decimal/timestamp representations yield an identical conclusion.
  4. submit a second request over the SAME trajectory that additionally uses
     per-period thresholds (threshold_periods) with a threshold switch, a
     crossing and a coverage gap, and assert the continuous excursions,
     degree-minute budget and locatable 422 semantics.

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


def staged_smoke_payload() -> dict:
    """Same trajectory as smoke_payload with per-period thresholds.

    Adds a threshold SWITCH at 00:30 (loading limit 8 degC, stabilized
    transport limit 6 degC) on top of the existing crossing (6->10 across 8)
    and coverage gap (00:25 -> 00:45, 1200s > 600s).
    """
    p = smoke_payload()
    p["max_single_excursion_seconds"] = 3600
    p["threshold_periods"] = [
        {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:30:00Z",
            "threshold_celsius": 8.0,
        },
        {
            "start": "2026-01-01T00:30:00Z",
            "end": "2026-01-01T00:55:00Z",
            "threshold_celsius": 6.0,
        },
    ]
    return p


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
    step("1/4 unit & integration tests (pytest)")
    check("pytest", run_cmd([sys.executable, "-m", "pytest", "-q", "tests"]) == 0)

    # 2. application build --------------------------------------------------
    step("2/4 application build check")
    check(
        "byte-compile",
        run_cmd([sys.executable, "-m", "compileall", "-q", "app", "verify"]) == 0,
    )
    check(
        "import app.main",
        run_cmd([sys.executable, "-c", "import app.main; print('import ok:', app.main.app.title)"]) == 0,
    )

    # 3. business smoke request ---------------------------------------------
    step("3/4 business smoke request (threshold crossing + coverage gap)")
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
        except requests.RequestException as exc:
            check("smoke request executed", False, str(exc))

    # 4. per-period thresholds: switch + crossing + gap ---------------------
    step("4/4 staged-threshold smoke request (switch + crossing + gap)")
    if not _failures:
        try:
            sp = staged_smoke_payload()
            rs = requests.post(API_BASE + "/api/cold-chain/exposure", json=sp, timeout=10)
            check("staged status 200", rs.status_code == 200, f"got {rs.status_code}: {rs.text[:400]}")
            sb = rs.json()

            check("staged verdict is fail", sb.get("verdict") == "fail", json.dumps(sb)[:400])
            check(
                "staged two continuous excursions, no double/missing timing",
                sb.get("excursions")
                == [
                    {
                        "start": "2026-01-01T00:05:00Z",
                        "end": "2026-01-01T00:22:30Z",
                        "duration_seconds": "1050",
                        "degree_minutes": "27.5",
                    },
                    {
                        "start": "2026-01-01T00:45:00Z",
                        "end": "2026-01-01T00:55:00Z",
                        "duration_seconds": "600",
                        "degree_minutes": "10",
                    },
                ],
                json.dumps(sb.get("excursions")),
            )
            check(
                "staged total degree-minutes 37.5 (period-aware budget)",
                sb.get("total_degree_minutes") == "37.5",
                str(sb.get("total_degree_minutes")),
            )
            scodes = {reason["code"] for reason in sb.get("reasons", [])}
            check(
                "staged reasons: gap + budget (stricter second period)",
                scodes == {"coverage_gap", "degree_minute_budget_exceeded"},
                f"codes={scodes}",
            )

            # Order invariance for the periods themselves.
            shuffled = staged_smoke_payload()
            shuffled["threshold_periods"] = list(reversed(shuffled["threshold_periods"]))
            rsv = requests.post(
                API_BASE + "/api/cold-chain/exposure", json=shuffled, timeout=10
            )
            check(
                "staged period-order invariant",
                rsv.status_code == 200 and rsv.json() == sb,
                f"status={rsv.status_code} body={rsv.text[:400]}",
            )

            # Boundary semantics: the new period's threshold takes effect at
            # the boundary; 9 degC vs 8 -> 9 thresholds closes the excursion
            # exactly at 00:30 with no duplicated or missed exposure.
            boundary = smoke_payload()
            boundary["max_interval_seconds"] = 3600
            boundary["max_single_excursion_seconds"] = 3600
            boundary["readings"] = [
                {"time": "2026-01-01T00:00:00Z", "celsius": 9.0},
                {"time": "2026-01-01T00:30:00Z", "celsius": 9.0},
                {"time": "2026-01-01T00:55:00Z", "celsius": 9.0},
            ]
            boundary["threshold_periods"] = [
                {
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-01T00:30:00Z",
                    "threshold_celsius": 8.0,
                },
                {
                    "start": "2026-01-01T00:30:00Z",
                    "end": "2026-01-01T00:55:00Z",
                    "threshold_celsius": 9.0,
                },
            ]
            rb = requests.post(
                API_BASE + "/api/cold-chain/exposure", json=boundary, timeout=10
            )
            check(
                "threshold jump closes/opens excursion at boundary (30 deg-min)",
                rb.status_code == 200
                and rb.json().get("excursions")
                == [
                    {
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-01-01T00:30:00Z",
                        "duration_seconds": "1800",
                        "degree_minutes": "30",
                    }
                ],
                f"status={rb.status_code} body={rb.text[:300]}",
            )

            # Locatable 422: a coverage hole between periods.
            badp = staged_smoke_payload()
            badp["threshold_periods"][1]["start"] = "2026-01-01T00:31:00Z"
            rh = requests.post(API_BASE + "/api/cold-chain/exposure", json=badp, timeout=10)
            hole_ok = (
                rh.status_code == 422
                and rh.json()["detail"][0]["loc"]
                == ["body", "threshold_periods", 1, "start"]
            )
            check("period coverage hole -> locatable 422", hole_ok, f"got {rh.status_code}: {rh.text[:300]}")
        except requests.RequestException as exc:
            check("staged smoke request executed", False, str(exc))

    step("summary")
    if _failures:
        print(f"VERIFY FAILED: {len(_failures)} check(s) failed: {', '.join(_failures)}", flush=True)
        return 1
    print("VERIFY OK: tests, build and smoke requests all passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
