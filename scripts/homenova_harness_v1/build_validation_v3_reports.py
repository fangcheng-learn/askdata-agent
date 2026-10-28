"""Offline reports for the newly authorized V3 trials; no model/network calls."""
from pathlib import Path

import build_latest_reports as reports


if __name__ == '__main__':
    reports.DIRECTORY = Path(__file__).resolve().parents[2] / 'data/homenova_harness_v1/private/validation_v3_real'
    reports.build()
