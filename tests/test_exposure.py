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
# core engine: per-period thresholds
# --------------------------------------------------------------------------
def _periods(spec):
    """spec: list of (start, end, threshold) already in Fractions."""
    return list(spec)


def test_periods_single_period_matches_constant_threshold():
    points = [(Fraction(0), Fraction(6)), (Fraction(600), Fraction(10))]
    single = compute_exposure(points, Fraction(8), Fraction(600))
    staged = compute_exposure(
        points, Fraction(99), Fraction(600), [(Fraction(0), Fraction(600), Fraction(8))]
    )
    assert [(e.start, e.end, e.degree_seconds) for e in staged.excursions] == [
        (e.start, e.end, e.degree_seconds) for e in single.excursions
    ]
    assert staged.total_degree_seconds == single.total_degree_seconds
    assert staged.gaps == single.gaps


def test_periods_threshold_switch_changes_exposure():
    # Constant temperature 9 degC. Threshold 10 in period 1 (no exposure),
    # threshold 8 in period 2 (exposure): the excursion opens exactly at the
    # boundary where the new threshold takes effect.
    points = [
        (Fraction(0), Fraction(9)),
        (Fraction(300), Fraction(9)),
        (Fraction(600), Fraction(9)),
    ]
    res = compute_exposure(
        points,
        Fraction(8),
        Fraction(600),
        [
            (Fraction(0), Fraction(300), Fraction(10)),
            (Fraction(300), Fraction(600), Fraction(8)),
        ],
    )
    assert not res.gaps
    assert [(e.start, e.end) for e in res.excursions] == [(300, 600)]
    # (9 - 8) degC over 300 s = 300 degree-seconds
    assert res.excursions[0].degree_seconds == 300


def test_periods_excursion_closes_when_new_threshold_not_exceeded():
    # 9 degC everywhere: above threshold 8 on both sides of t=300, but the
    # new period's threshold is 9 -> strictly above is false at/after the
    # boundary, so the excursion ends at 300.
    points = [
        (Fraction(0), Fraction(9)),
        (Fraction(300), Fraction(9)),
        (Fraction(600), Fraction(9)),
    ]
    res = compute_exposure(
        points,
        Fraction(8),
        Fraction(600),
        [
            (Fraction(0), Fraction(300), Fraction(8)),
            (Fraction(300), Fraction(600), Fraction(9)),
        ],
    )
    assert [(e.start, e.end) for e in res.excursions] == [(0, 300)]
    assert res.excursions[0].degree_seconds == 300  # 1 degC * 300 s


def test_periods_strictly_above_both_sides_keeps_one_excursion():
    # Temperature 9 at the boundary; thresholds 8 -> 8.5, both exceeded:
    # one continuous excursion even though the applicable threshold jumps.
    points = [
        (Fraction(0), Fraction(9)),
        (Fraction(300), Fraction(9)),
        (Fraction(600), Fraction(9)),
    ]
    res = compute_exposure(
        points,
        Fraction(8),
        Fraction(600),
        [
            (Fraction(0), Fraction(300), Fraction(8)),
            (Fraction(300), Fraction(600), Fraction(Fraction(17, 2))),
        ],
    )
    assert len(res.excursions) == 1
    assert (res.excursions[0].start, res.excursions[0].end) == (0, 600)
    # (9-8)*300 + (9-8.5)*300 = 300 + 150 degree-seconds, no double counting
    assert res.excursions[0].degree_seconds == 450


def test_periods_crossing_solved_within_split_segment():
    # One sampling segment [0, 600], temperature linear 6 -> 12; threshold
    # switches 8 -> 11 at t=300 (temperature there is exactly 9).
    # Period 1: exposed where v>8 -> from t=200 (v=8) to 300.
    # Period 2: exposed where v>11 -> from t=500 (v=11) to 600.
    # Two separate excursions; each piece integrated against its own
    # threshold.
    points = [(Fraction(0), Fraction(6)), (Fraction(600), Fraction(12))]
    res = compute_exposure(
        points,
        Fraction(8),
        Fraction(600),
        [
            (Fraction(0), Fraction(300), Fraction(8)),
            (Fraction(300), Fraction(600), Fraction(11)),
        ],
    )
    assert [(e.start, e.end) for e in res.excursions] == [(200, 300), (500, 600)]
    # piece 1 triangle: excess 0..(9-8)=1 over 100 s -> 50 deg*s
    assert res.excursions[0].degree_seconds == 50
    # piece 2 triangle: excess 0..(12-11)=1 over 100 s -> 50 deg*s
    assert res.excursions[1].degree_seconds == 50
    assert res.total_degree_seconds == 100


def test_periods_gap_not_counted_across():
    # A coverage gap [600,1800] straddles the period boundary at 1200:
    # no exposure across it, and excursions on either side stay separate.
    points = [
        (Fraction(0), Fraction(10)),
        (Fraction(600), Fraction(10)),
        (Fraction(1800), Fraction(10)),
        (Fraction(2400), Fraction(10)),
    ]
    res = compute_exposure(
        points,
        Fraction(8),
        Fraction(600),
        [
            (Fraction(0), Fraction(1200), Fraction(8)),
            (Fraction(1200), Fraction(2400), Fraction(9)),
        ],
    )
    assert [g.duration_seconds for g in res.gaps] == [1200]
    assert [(e.start, e.end) for e in res.excursions] == [(0, 600), (1800, 2400)]
    # second excursion against the stricter 9 degC threshold: 1 degC * 600 s
    assert res.excursions[1].degree_seconds == 600


def test_periods_input_order_does_not_matter():
    points = [
        (Fraction(0), Fraction(9)),
        (Fraction(300), Fraction(9)),
        (Fraction(600), Fraction(9)),
    ]
    periods_a = [
        (Fraction(0), Fraction(300), Fraction(10)),
        (Fraction(300), Fraction(600), Fraction(8)),
    ]
    periods_b = list(reversed(periods_a))
    ra = compute_exposure(points, Fraction(8), Fraction(600), periods_a)
    rb = compute_exposure(points, Fraction(8), Fraction(600), periods_b)
    assert [(e.start, e.end, e.degree_seconds) for e in ra.excursions] == [
        (e.start, e.end, e.degree_seconds) for e in rb.excursions
    ]


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


# --------------------------------------------------------------------------
# API: per-period thresholds (threshold_periods)
# --------------------------------------------------------------------------
def staged_smoke_payload():
    """Switch + crossing + coverage gap in one trajectory.

    Timeline (all readings every 600s except 00:25->00:45 = 1200s gap):
      00:00 v=6   00:10 v=10  00:20 v=10  00:25 v=6
      [gap 1200s]
      00:45 v=7   00:55 v=7
    Periods: loading 00:00-00:30 threshold 8, transport 00:30-00:55 threshold 6.
    """
    p = smoke_payload()
    p["max_single_excursion_seconds"] = 3600
    p["degree_minute_budget"] = 30
    p["threshold_periods"] = [
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:30:00Z",
         "threshold_celsius": 8.0},
        {"start": "2026-01-01T00:30:00Z", "end": "2026-01-01T00:55:00Z",
         "threshold_celsius": 6.0},
    ]
    return p


def test_api_periods_switch_crossing_and_gap():
    # Period 1 (threshold 8): exposed 00:05:00 -> 00:22:30 (crossing 6->10
    # up, 10->6 down), 27.5 degree-minutes.
    # Gap 00:25 -> 00:45.
    # Period 2 (threshold 6): 00:45 v=7 -> 00:55 v=7, entire segment exposed,
    # (7-6) * 600 / 60 = 10 degree-minutes.  Separate excursion (gap breaks).
    r = client.post("/api/cold-chain/exposure", json=staged_smoke_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "fail"
    assert body["coverage_gaps"] == [
        {
            "start": "2026-01-01T00:25:00Z",
            "end": "2026-01-01T00:45:00Z",
            "duration_seconds": "1200",
        }
    ]
    assert body["excursions"] == [
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
    ]
    # 27.5 + 10 = 37.5 > 30 budget: stricter second period tips the verdict
    assert body["total_degree_minutes"] == "37.5"
    codes = [reason["code"] for reason in body["reasons"]]
    assert codes == ["coverage_gap", "degree_minute_budget_exceeded"]


def test_api_periods_same_trajectory_other_thresholds_pass():
    # The identical trajectory with a looser second-period threshold (8):
    # period 2 contributes nothing, total stays 27.5 <= 30 -> only the gap
    # fails.  Shows the conclusion changes with the per-period limits.
    p = staged_smoke_payload()
    p["threshold_periods"][1]["threshold_celsius"] = 8.0
    body = client.post("/api/cold-chain/exposure", json=p).json()
    assert body["total_degree_minutes"] == "27.5"
    assert [e["end"] for e in body["excursions"]] == ["2026-01-01T00:22:30Z"]
    codes = [reason["code"] for reason in body["reasons"]]
    assert codes == ["coverage_gap"]


def test_api_periods_boundary_threshold_new_period_in_effect():
    # Reading exactly at the boundary equals the NEW period's threshold:
    # not exposed there, excursion ends at the boundary (no overlap/double
    # timing). 9 -> 9 -> 9 constant; thresholds 8 then 9.
    p = smoke_payload()
    p["max_interval_seconds"] = 3600
    p["readings"] = [
        {"time": "2026-01-01T00:00:00Z", "celsius": 9.0},
        {"time": "2026-01-01T00:30:00Z", "celsius": 9.0},
        {"time": "2026-01-01T00:55:00Z", "celsius": 9.0},
    ]
    p["threshold_periods"] = [
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:30:00Z",
         "threshold_celsius": 8.0},
        {"start": "2026-01-01T00:30:00Z", "end": "2026-01-01T00:55:00Z",
         "threshold_celsius": 9.0},
    ]
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["excursions"] == [
        {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:30:00Z",
            "duration_seconds": "1800",
            "degree_minutes": "30",  # (9-8)*1800/60
        }
    ]
    assert body["total_degree_minutes"] == "30"


def test_api_periods_order_invariant():
    p1 = staged_smoke_payload()
    p2 = staged_smoke_payload()
    p2["threshold_periods"] = list(reversed(p2["threshold_periods"]))
    r1 = client.post("/api/cold-chain/exposure", json=p1)
    r2 = client.post("/api/cold-chain/exposure", json=p2)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_api_periods_not_an_array_422():
    p = staged_smoke_payload()
    p["threshold_periods"] = {"start": "2026-01-01T00:00:00Z"}
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "threshold_periods"]


def test_api_periods_count_bounds_422():
    p = staged_smoke_payload()
    p["threshold_periods"] = []
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["loc"] == ["body", "threshold_periods"]
    assert "1 and 16" in detail["msg"]

    p = staged_smoke_payload()
    p["threshold_periods"] = [
        {
            "start": f"2026-01-01T00:{i:02d}:00Z" if i < 56 else "2026-01-01T00:55:00Z",
            "end": (
                f"2026-01-01T00:{i + 1:02d}:00Z"
                if i + 1 < 56
                else "2026-01-01T00:55:00Z"
            ),
            "threshold_celsius": 8.0,
        }
        for i in range(17)
    ]
    # The fabricated instants need not be valid; the count error is raised
    # before per-item validation, so just assert the locatable failure.
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "threshold_periods"]


def test_api_periods_missing_inner_field_422():
    p = staged_smoke_payload()
    del p["threshold_periods"][1]["threshold_celsius"]
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == [
        "body", "threshold_periods", 1, "threshold_celsius"
    ]


def test_api_periods_bad_timestamp_422():
    p = staged_smoke_payload()
    p["threshold_periods"][0]["end"] = "not-a-time"
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == [
        "body", "threshold_periods", 0, "end"
    ]


def test_api_periods_non_finite_threshold_422():
    p = staged_smoke_payload()
    raw = json.dumps(p).replace(
        '"threshold_celsius": 6.0', '"threshold_celsius": Infinity', 1
    )
    # Replace only within threshold_periods (the second period carries 6.0).
    r = client.post(
        "/api/cold-chain/exposure",
        content=raw.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(
        d["loc"] == ["body", "threshold_periods", 1, "threshold_celsius"]
        and "finite" in d["msg"]
        for d in detail
    ), detail


def test_api_periods_reversed_single_period_422():
    p = staged_smoke_payload()
    p["threshold_periods"][1]["start"] = "2026-01-01T00:55:00Z"
    p["threshold_periods"][1]["end"] = "2026-01-01T00:30:00Z"
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == [
        "body", "threshold_periods", 1, "end"
    ]


def test_api_periods_does_not_cover_start_422():
    p = staged_smoke_payload()
    p["threshold_periods"][0]["start"] = "2026-01-01T00:01:00Z"
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    # Error reported at the earliest period in absolute order; carries the
    # ORIGINAL index even when inputs arrive shuffled.
    assert detail["loc"][:2] == ["body", "threshold_periods"]
    assert "transport_start" in detail["msg"]


def test_api_periods_overlap_422():
    p = staged_smoke_payload()
    p["threshold_periods"][1]["start"] = "2026-01-01T00:29:00Z"  # overlaps
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["loc"] == ["body", "threshold_periods", 1, "start"]
    assert "overlaps" in detail["msg"]


def test_api_periods_hole_422():
    p = staged_smoke_payload()
    p["threshold_periods"][1]["start"] = "2026-01-01T00:31:00Z"  # 60s hole
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["loc"] == ["body", "threshold_periods", 1, "start"]
    assert "coverage hole" in detail["msg"]


def test_api_periods_overlap_error_uses_original_index_when_shuffled():
    p = staged_smoke_payload()
    # Submit second period first in the array; overlap is period[0] vs
    # period[1] and must be reported at the later period's input index.
    p["threshold_periods"] = [
        {"start": "2026-01-01T00:30:00Z", "end": "2026-01-01T00:55:00Z",
         "threshold_celsius": 6.0},
        {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:31:00Z",
         "threshold_celsius": 8.0},
    ]
    r = client.post("/api/cold-chain/exposure", json=p)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["loc"] == ["body", "threshold_periods", 1, "end"] or (
        detail["loc"] == ["body", "threshold_periods", 0, "start"]
        and "overlaps" in detail["msg"]
    )


def test_api_omitting_periods_keeps_legacy_semantics():
    # No threshold_periods: response must be byte-for-byte the legacy one.
    r = client.post("/api/cold-chain/exposure", json=smoke_payload())
    assert r.status_code == 200
    assert r.json() == {
        "verdict": "fail",
        "reasons": [
            {
                "code": "coverage_gap",
                "gap_index": 0,
                "message": (
                    "coverage gap from 2026-01-01T00:25:00Z to "
                    "2026-01-01T00:45:00Z (1200s) exceeds max_interval_seconds 600"
                ),
            },
            {
                "code": "single_excursion_exceeded",
                "excursion_index": 0,
                "duration_seconds": "1050",
                "limit_seconds": "900",
                "message": (
                    "excursion 0 lasts 1050s, exceeding max_single_excursion_seconds 900"
                ),
            },
        ],
        "excursions": [
            {
                "start": "2026-01-01T00:05:00Z",
                "end": "2026-01-01T00:22:30Z",
                "duration_seconds": "1050",
                "degree_minutes": "27.5",
            }
        ],
        "total_degree_minutes": "27.5",
        "coverage_gaps": [
            {
                "start": "2026-01-01T00:25:00Z",
                "end": "2026-01-01T00:45:00Z",
                "duration_seconds": "1200",
            }
        ],
    }


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
