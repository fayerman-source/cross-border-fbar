# The hosted demo: fictional statements and recorded readings, with no model calls and no API key.
# Serve it behind a proxy that terminates HTTPS and passes the Host header through. deploy/hosted-demo.md
# has the details. The public host comes from the environment at run time: FBAR_PUBLIC_HOST, or SPACE_HOST
# on Hugging Face. The container exits at startup if neither is set.
FROM docker.io/library/python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_ROOT_USER_ACTION=ignore UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.10.0 && useradd --create-home --uid 1000 demo

WORKDIR /app
# Exactly the locked dependencies, with no dev tools. The project itself runs from src/, so nothing is
# built with unlocked build tools.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY LICENSE ./
COPY src/ ./src/
COPY results/muse-spark-1.3_synth.json ./results/
ENV PYTHONPATH=/app/src

# The synthetic set is generated here, so its page images are the ones the readings were recorded from.
# The app reads only the page images and the intake file; the answer key doesn't ship.
RUN .venv/bin/python -m cross_border_fbar.synth --out out/synth --png \
    && rm -rf out/synth/truth.json out/synth/statements

# Everything above belongs to root, so the running app can't change what visitors share. Each visitor's
# temporary worksheet goes under the system temp folder.
USER demo
EXPOSE 7860
CMD [".venv/bin/python", "-m", "cross_border_fbar.app", "--hosted"]
