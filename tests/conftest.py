"""One generated synthetic set for the test session: generating it takes about half a minute."""

from pathlib import Path

import pytest

from cross_border_fbar.app import phrases
from cross_border_fbar.app.worksheet import Inputs
from cross_border_fbar.synth.__main__ import generate

READINGS = Path(__file__).resolve().parents[1] / "results" / "muse-spark-1.3_synth.json"


@pytest.fixture(scope="session")
def inputs(tmp_path_factory) -> Inputs:
    out = tmp_path_factory.mktemp("synth")
    generate(out, 2025, png=True, dpi=150, threshold_usd=1000)
    return Inputs(out, READINGS, out / "intake.json")


@pytest.fixture
def heading_unreviewed(tmp_path, monkeypatch) -> str:
    """The committed catalog with its heading back in draft, to exercise the unreviewed-phrase path."""
    catalog = phrases.load("ko")
    message = catalog.get(phrases.current_phrases()["heading"], context="heading")
    message.flags.add("fuzzy")
    monkeypatch.setattr(phrases, "PHRASES_DIR", tmp_path / "catalog")
    phrases.save("ko", catalog)
    return message.string
