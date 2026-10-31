"""A built, non-editable wheel must contain the data its installed code reads."""

import os
import subprocess
import sys
from pathlib import Path


def test_installed_wheel_loads_rates_and_renders_with_packaged_fonts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("*.whl"))
    target = tmp_path / "installed"
    env = {
        **os.environ,
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    subprocess.run(
        ["uv", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import cross_border_fbar
from cross_border_fbar import rates
from cross_border_fbar.app import phrases
from cross_border_fbar.app.server import FONTS_DIR
from cross_border_fbar.synth.__main__ import FONTS
from cross_border_fbar.synth.household import household
from cross_border_fbar.synth.ledger import build_ledger, monthly_statements
from cross_border_fbar.synth.render import render_statement, static_font
assert Path(cross_border_fbar.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
loaded = rates.load(2025)
assert {'KRW', 'EUR', 'THB'} <= set(loaded)
assert len(phrases.reviewed('ko', phrases.current_phrases())) == 27
assert all(rate.record_date == '2025-12-31' and rate.source_url.startswith(rates.API_URL)
           for rate in loaded.values())
font_names = ('NotoSans-VF.ttf', 'NotoSansKR-VF.ttf', 'NotoSansThai-VF.ttf')
assert FONTS == FONTS_DIR and all((FONTS / name).is_file() for name in font_names)
assert static_font(FONTS, 'latin', 400).is_file()
spec = next(account for account in household(2025) if account.account_id == 'de-joint')
statement = monthly_statements(spec, 2025, build_ledger(spec, 2025, 2025))[0]
pdf = Path('installed-wheel-synthetic.pdf')
render_statement(spec, statement, FONTS, pdf)
assert pdf.is_file() and pdf.stat().st_size > 0
"""
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(target)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
