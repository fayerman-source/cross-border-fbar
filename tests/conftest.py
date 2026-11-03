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


@pytest.fixture(autouse=True)
def reviewed_catalog(request, tmp_path_factory, monkeypatch):
    """Most tests exercise the reviewed path, so they get a copy of the catalog marked reviewed. Tests of the
    committed catalog itself opt out with @pytest.mark.committed_catalog."""
    if request.node.get_closest_marker("committed_catalog"):
        return
    folder = tmp_path_factory.getbasetemp() / "reviewed-catalog"
    if not folder.exists():
        catalog = phrases.load("ko")
        catalog.last_translator = "test reviewer"
        for message in catalog:
            if message.id:
                message.flags.discard("fuzzy")
        with monkeypatch.context() as m:
            m.setattr(phrases, "PHRASES_DIR", folder)
            phrases.save("ko", catalog)
    monkeypatch.setattr(phrases, "PHRASES_DIR", folder)


@pytest.fixture
def heading_unreviewed(tmp_path, monkeypatch) -> str:
    """The committed catalog with its heading back in draft, to exercise the unreviewed-phrase path."""
    catalog = phrases.load("ko")
    message = catalog.get(phrases.current_phrases()["heading"], context="heading")
    message.flags.add("fuzzy")
    monkeypatch.setattr(phrases, "PHRASES_DIR", tmp_path / "catalog")
    phrases.save("ko", catalog)
    return message.string
