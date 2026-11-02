"""On-screen wording stays plain: no em-dashes in anything the app shows."""

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src" / "cross_border_fbar" / "app"


def test_no_em_dash_in_app_text():
    files = [*APP.glob("*.py"), *(APP / "static").glob("*.js"), *(APP / "static").glob("*.html")]
    assert files
    assert [f.name for f in files if "\u2014" in f.read_text(encoding="utf-8")] == []
