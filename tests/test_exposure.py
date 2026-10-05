"""Unit and API tests for the cold-chain exposure service."""
from __future__ import annotations

import json
import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.core import compute_exposure
from app.main import app
from app.timeutil import (
    fraction_to_decimal_str,
    fraction_to_rfc3339,
    parse_rfc3339,
)

client = TestClient(app)

H = 3600  # seconds per hour


# --------------------------------------------------------------------------
# time parsing / formatting
# --------------------------------------------------------------------------
def test_parse_equivalent_representations_same_instant():
    a = parse_rfc3339("2026-01-01T00:00:00Z")
    b = parse_rfc3339("2026-01-01T00:00:00.000Z")
    c = parse_rfc3339("2026-01-01T02:00:00+02:00")
    d = parse_rfc3339("2025-12-31T19:00:00.000000000-05:00")
    assert a == b == c == d


def test_parse_fractional_seconds_exact():
    t = parse_rfc3339("2026-01-01T00:00:00.25Z") - parse_rfc3339("2026-01-01T00:00:00Z")
    assert t == Fraction(1, 4)


@pytest.mark.parametrize(
    "bad",
    [
        "2026-13-01T00:00:00Z",  # month 13
        "2026-01-32T00:00:00Z",  # day 32
        "2026-01-01T24:00:00Z",  # hour 24
        "2026-01-01T00:00:60Z",  # leap second unsupported
        "2026-01-01 00:00:00Z",  # missing T
        "2026-01-01T00:00:00",  # missing offset
        "not-a-time",
    ],
)
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_rfc3339(bad)


def test_decimal_formatting_exact_and_rounded():
    assert fraction_to_decimal_str(Fraction(55, 2)) == "27.5"
    assert fraction_to_decimal_str(Fraction(1050)) == "1050"
    assert fraction_to_decimal_str(Fraction(1, 3)) == "0.333333333"
    assert fraction_to_decimal_str(Fraction(0)) == "0"


def test_rfc3339_roundtrip():
    t = parse_rfc3339("2026-03-04T05:06:07.123456789Z")
    assert fraction_to_rfc3339(t) == "2026-03-04T05:06:07.123456789Z"
    assert fraction_to_rfc3339(parse_rfc3339("2026-03-04T05:06:07Z")) == (
        "2026-03-04T05:06:07Z"
    )


# --------------------------------------------------------------------------
# core engine
# --------------------------------------------------------------------------
def test_crossing_point_exact():
    # 6 -> 10 degC over 600s, threshold 8: crossing exactly halfway (t=300).
    res = compute_exposure(
        [(Fraction(0), Fraction(6)), (Fraction(600), Fraction(10))],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert not res.gaps
    assert len(res.excursions) == 1
    exc = res.excursions[0]
    assert exc.start == 300 and exc.end == 600
    assert exc.duration_seconds == 300
    assert exc.degree_seconds == 300  # triangle: 2 degC * 300 s / 2
    assert exc.degree_minutes == 5


def test_gap_blocks_interpolation():
    # Both ends above threshold but the gap is too wide: no exposure, one gap.
    res = compute_exposure(
        [(Fraction(0), Fraction(10)), (Fraction(1200), Fraction(10))],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert len(res.gaps) == 1
    assert res.gaps[0].duration_seconds == 1200
    assert res.excursions == []
    assert res.total_degree_minutes == 0


def test_gap_breaks_excursion():
    res = compute_exposure(
        [
            (Fraction(0), Fraction(10)),
            (Fraction(600), Fraction(10)),
            (Fraction(1800), Fraction(10)),  # 1200s gap before this
            (Fraction(2400), Fraction(10)),
        ],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert [g.duration_seconds for g in res.gaps] == [1200]
    assert [(e.start, e.end) for e in res.excursions] == [(0, 600), (1800, 2400)]


def test_equal_to_threshold_is_not_exposure():
    res = compute_exposure(
        [(Fraction(0), Fraction(8)), (Fraction(600), Fraction(8))],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert res.excursions == []
    assert res.total_degree_minutes == 0


def test_touching_threshold_does_not_split_excursion():
    # 10 -> 8 -> 10: the middle reading equals the threshold (not exposed at
    # that instant) but the excursion is continuous through it.
    res = compute_exposure(
        [
            (Fraction(0), Fraction(10)),
            (Fraction(600), Fraction(8)),
            (Fraction(1200), Fraction(10)),
        ],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert len(res.excursions) == 1
    assert res.excursions[0].duration_seconds == 1200
    # two triangles of 2 degC * 600 s / 2 each
    assert res.excursions[0].degree_seconds == 1200


def test_merge_across_shared_reading():
    res = compute_exposure(
        [
            (Fraction(0), Fraction(10)),
            (Fraction(600), Fraction(10)),
            (Fraction(1200), Fraction(6)),
        ],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert len(res.excursions) == 1
    exc = res.excursions[0]
    assert (exc.start, exc.end) == (0, 900)  # crossing halfway down 10 -> 6
    assert exc.degree_seconds == 1200 + 300


def test_interval_exactly_at_max_is_not_a_gap():
    res = compute_exposure(
        [(Fraction(0), Fraction(10)), (Fraction(600), Fraction(10))],
        threshold=Fraction(8),
        max_interval=Fraction(600),
    )
    assert not res.gaps
    assert len(res.excursions) == 1


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def smoke_payload():
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
            {"time": "2026-01-01T00:45:00Z", "celsius": 7.0},  # 1200s gap
            {"time": "2026-01-01T00:55:00Z", "celsius": 7.0},  # 600s: exactly at limit
        ],
    }


def test_api_smoke_crossing_plus_gap():
    r = client.post("/api/cold-chain/exposure", json=smoke_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "fail"
    codes = [reason["code"] for reason in body["reasons"]]
    assert "coverage_gap" in codes
    assert "single_excursion_exceeded" in codes
    assert "degree_minute_budget_exceeded" not in codes  # 27.5 <= 30

    assert body["excursions"] == [
        {
            "start": "2026-01-01T00:05:00Z",
            "end": "2026-01-01T00:22:30Z",
            "duration_seconds": "1050",
            "degree_minutes": "27.5",
        }
    ]
    assert body["total_degree_minutes"] == "27.5"
    assert body["coverage_gaps"] == [
        {
            "start": "2026-01-01T00:25:00Z",
            "end": "2026-01-01T00:45:00Z",
            "duration_seconds": "1200",
        }
    ]


def test_api_pass_verdict():
    payload = smoke_payload()
    payload["max_interval_seconds"] = 3600  # no gap anymore
    payload["max_single_excursion_seconds"] = 3600
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "pass"
    assert body["reasons"] == []
    assert body["coverage_gaps"] == []


def test_api_budget_exceeded():
    payload = smoke_payload()
    payload["max_interval_seconds"] = 3600
    payload["max_single_excursion_seconds"] = 3600
    payload["degree_minute_budget"] = 10
    r = client.post("/api/cold-chain/exposure", json=payload)
    body = r.json()
    assert body["verdict"] == "fail"
    assert [reason["code"] for reason in body["reasons"]] == [
        "degree_minute_budget_exceeded"
    ]


def test_api_order_and_representation_invariant():
    """Shuffled readings + different decimal/offset spellings -> same result."""
    p1 = smoke_payload()
    p2 = smoke_payload()
    p2["threshold_celsius"] = 8.00  # same value, more zeros
    p2["degree_minute_budget"] = 30.00
    alt = [
        {"time": "2026-01-01T00:00:00.000Z", "celsius": 6.00},
        {"time": "2026-01-01T02:10:00+02:00", "celsius": 10.000},  # == 00:10Z
        {"time": "2026-01-01T00:20:00Z", "celsius": 10.0},
        {"time": "2026-01-01T00:25:00.000000Z", "celsius": 6},
        {"time": "2026-01-01T00:45:00Z", "celsius": 7.0},
        {"time": "2026-01-01T00:55:00Z", "celsius": 7.00},
    ]
    random.Random(42).shuffle(alt)
    p2["readings"] = alt

    r1 = client.post("/api/cold-chain/exposure", json=p1)
    r2 = client.post("/api/cold-chain/exposure", json=p2)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_api_duplicate_timestamp_422():
    payload = smoke_payload()
    payload["readings"][1]["time"] = "2026-01-01T00:00:00.000Z"  # == readings[0]
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "readings", 1, "time"]
    assert "duplicate" in detail[0]["msg"]
    assert "readings[0]" in detail[0]["msg"]


def test_api_invalid_timestamp_422():
    payload = smoke_payload()
    payload["readings"][2]["time"] = "2026-01-01 00:20:00"  # not RFC3339
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "readings", 2, "time"]


def test_api_non_finite_number_422():
    payload = smoke_payload()
    raw = json.dumps(payload).replace('"threshold_celsius": 8.0', '"threshold_celsius": NaN')
    assert "NaN" in raw
    r = client.post(
        "/api/cold-chain/exposure",
        content=raw.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "threshold_celsius"]
    assert "finite" in detail[0]["msg"]


def test_api_non_finite_reading_422():
    payload = smoke_payload()
    raw = json.dumps(payload).replace('"celsius": 10.0', '"celsius": Infinity', 1)
    r = client.post(
        "/api/cold-chain/exposure",
        content=raw.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "readings", 1, "celsius"]


def test_api_boundary_mismatch_422():
    payload = smoke_payload()
    payload["readings"][0]["time"] = "2026-01-01T00:01:00Z"  # not transport_start
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "readings", 0, "time"]
    assert "transport_start" in detail[0]["msg"]


def test_api_reading_count_bounds_422():
    payload = smoke_payload()
    payload["readings"] = payload["readings"][:1]
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "readings"]


def test_api_missing_field_422():
    payload = smoke_payload()
    del payload["threshold_celsius"]
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "threshold_celsius"]


def test_api_end_before_start_422():
    payload = smoke_payload()
    payload["transport_end"] = "2025-12-31T23:00:00Z"
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "transport_end"]


def test_api_limits_are_strict():
    """Exactly at a limit is not a violation (only exceeding is)."""
    payload = smoke_payload()
    payload["max_interval_seconds"] = 1200  # gap interval exactly at limit
    payload["max_single_excursion_seconds"] = 1050  # excursion exactly at limit
    payload["degree_minute_budget"] = 27.5  # total exactly at budget
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "pass"
    assert body["reasons"] == []
    assert body["coverage_gaps"] == []  # 1200s no longer exceeds the limit


def test_api_too_many_readings_422():
    payload = smoke_payload()
    t0 = parse_rfc3339("2026-01-01T00:00:00Z")
    payload["transport_end"] = "2026-01-01T08:20:00Z"
    payload["readings"] = [
        {"time": fraction_to_rfc3339(t0 + 60 * i), "celsius": 5.0} for i in range(501)
    ]
    r = client.post("/api/cold-chain/exposure", json=payload)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "readings"]


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
