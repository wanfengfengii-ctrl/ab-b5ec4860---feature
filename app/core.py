"""Cold-chain thermal exposure engine.

All arithmetic uses ``fractions.Fraction`` so every crossing point, duration
and degree-minute total is exact and independent of how the input decimals
were written.

Rules implemented here:
* Readings are evaluated in absolute-time order (caller sorts first).
* Between two adjacent readings the temperature is assumed to vary linearly,
  but only when the gap between them does not exceed ``max_interval``.
* A wider gap is a *coverage gap*: no interpolation happens across it and it
  breaks any excursion in progress.
* Only temperatures *strictly* above the threshold count as exposure; a
  reading exactly equal to the threshold contributes nothing.
* Threshold crossings are solved exactly (linear interpolation), and the
  exposed sub-intervals plus their degree-minute areas (trapezoid/triangle
  integration of degrees above threshold over time) are accumulated.
* Exposed pieces touching at a shared reading merge into one excursion.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Excursion:
    """One continuous interval with temperature strictly above threshold."""

    start: Fraction  # epoch seconds
    end: Fraction  # epoch seconds
    degree_seconds: Fraction  # integral of (celsius - threshold) over [start, end]

    @property
    def duration_seconds(self) -> Fraction:
        return self.end - self.start

    @property
    def degree_minutes(self) -> Fraction:
        return self.degree_seconds / 60


@dataclass(frozen=True)
class Gap:
    """An interval with no trustworthy coverage (sampling gap)."""

    start: Fraction
    end: Fraction

    @property
    def duration_seconds(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True)
class ExposureResult:
    excursions: list  # list[Excursion], time-ordered
    gaps: list  # list[Gap], time-ordered
    total_degree_seconds: Fraction

    @property
    def total_degree_minutes(self) -> Fraction:
        return self.total_degree_seconds / 60


def compute_exposure(
    points: list,  # list[(Fraction, Fraction)] sorted by time
    threshold: Fraction,
    max_interval: Fraction,
) -> ExposureResult:
    pieces: list = []
    gaps: list = []

    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt > max_interval:
            # Coverage gap: no interpolation across this interval.
            gaps.append(Gap(t0, t1))
            continue

        a = v0 - threshold  # excess above threshold at segment start
        b = v1 - threshold  # excess at segment end

        if a <= 0 and b <= 0:
            continue  # never strictly above threshold

        if a > 0 and b > 0:
            # Whole segment exposed: trapezoid area.
            pieces.append(Excursion(t0, t1, (a + b) * dt / 2))
            continue

        # Exactly one end is above threshold: solve the crossing point
        # exactly, then integrate the exposed triangle.
        f = a / (a - b)  # fraction of the segment where v == threshold
        t_cross = t0 + f * dt
        if a > 0:
            pieces.append(Excursion(t0, t_cross, a * (t_cross - t0) / 2))
        else:
            pieces.append(Excursion(t_cross, t1, b * (t1 - t_cross) / 2))

    # Merge pieces that touch at a shared reading (both sides exposed).
    merged: list = []
    for piece in pieces:
        if merged and merged[-1].end == piece.start:
            last = merged[-1]
            merged[-1] = Excursion(
                last.start, piece.end, last.degree_seconds + piece.degree_seconds
            )
        else:
            merged.append(piece)

    total = sum((p.degree_seconds for p in merged), Fraction(0))
    return ExposureResult(merged, gaps, total)
