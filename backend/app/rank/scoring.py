"""Incident scoring. All weights live here so they can be tuned live on sprint day."""

from __future__ import annotations

WEIGHTS = {
    "burst": 0.35,         # how far above baseline the signals spiked
    "severity": 0.25,      # share of ERROR/CRITICAL observations
    "blast_radius": 0.25,  # number of distinct services and hosts
    "duration": 0.15,      # how long the incident lasted
}
