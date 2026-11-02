"""The container image for the hosted demo holds only what it needs and runs as a non-root user."""

import re
from pathlib import Path


def test_the_container_recipe_keeps_to_the_limits():
    root = Path(__file__).resolve().parents[1]
    recipe = [line.strip() for line in (root / "Dockerfile").read_text(encoding="utf-8").splitlines()]
    steps = [line for line in recipe if line and not line.startswith("#")]
    assert "@sha256:" in steps[0]  # a fixed base image
    assert any("uv sync --locked --no-dev" in line for line in steps)
    assert steps[-3:] == [
        "USER demo",
        "EXPOSE 7860",
        'CMD [".venv/bin/python", "-m", "cross_border_fbar.app", "--hosted"]',
    ]
    assert not any(re.search(r"KEY|SECRET|TOKEN", line) for line in steps)
    allowed = [
        line
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.startswith("!")
    ]
    ignore = [
        line
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line and line[0] != "#"
    ]
    assert ignore[0] == "*"  # nothing reaches the image unless it is named here
    assert not any(name in line for line in allowed for name in ("recordings", ".env", "tests", "out"))
