"""One generated synthetic set for the test session: generating it takes about half a minute."""

from pathlib import Path

import pytest

from cross_border_fbar.app.worksheet import Inputs
from cross_border_fbar.synth.__main__ import generate

READINGS = Path(__file__).resolve().parents[1] / "results" / "muse-spark-1.3_synth.json"


@pytest.fixture(scope="session")
def inputs(tmp_path_factory) -> Inputs:
    out = tmp_path_factory.mktemp("synth")
    generate(out, 2025, png=True, dpi=150, threshold_usd=1000)
    return Inputs(out, READINGS, out / "intake.json")
