# Hosted demo

The hosted demo is the real review app with the working controls: source pages, corrections, sign-off and the client summary. It runs on fictional statements and recorded readings. It has no API key and makes no model calls.

It needs a host that runs a container and terminates HTTPS. The steps for Hugging Face are at the end; nothing in the app or the Dockerfile requires Hugging Face.

## Run the container

```bash
docker build -t fbar-demo .
docker run -p 7860:7860 -e FBAR_PUBLIC_HOST=demo.example.org fbar-demo
```

| Setting | Where it comes from | If it is missing |
| --- | --- | --- |
| Public host | `--public-host`, then `FBAR_PUBLIC_HOST`, then `SPACE_HOST` | The app exits at startup. It never guesses. |
| Port | `--port`, then `PORT` | 7860 |

The proxy in front has two jobs:

- **Terminate HTTPS.** The app itself speaks plain HTTP. Don't expose its port directly: browsers won't keep the visitor cookie without HTTPS, so every action would report a reset session.
- **Pass the browser's Host header through unchanged.** Every request whose Host isn't exactly the public host gets a 403. `X-Forwarded-*` headers are never read, because a client can send those too. Each refusal is logged as `refused GET: host='...' origin='...'`.

Running the app with no flags is the local app, unchanged: it binds 127.0.0.1 and ignores all of these settings.

## What each visitor gets

Each browser gets its own worksheet and its own folder under the system temp folder, found through a random `HttpOnly`, `Secure`, `SameSite=Strict` cookie. One visitor's notes, corrections and sign-off never appear for another. The shared inputs belong to root in the image, and the app runs as a non-root user, so it can't change them.

Worksheets are deleted when:

- the visitor has been idle for 2 hours
- 100 visitors are active and a new one arrives, in which case the least recently used goes first
- the container restarts

A visitor who comes back after that gets a fresh worksheet and a message saying so. An action sent from the old page isn't applied.

Anyone can make the demo forget other visitors' work by opening 100 new sessions. That costs a visitor their demo progress and nothing else, since the data is fictional and each person can start again.

| Limit | Value |
| --- | --- |
| Request body | 16 KiB |
| Note | 2,000 characters |
| Any other text field | 200 characters |
| Disk per visitor | 512 KiB, so about 60 MB in all; past it, changes are refused and the worksheet stays readable |
| Connections at once | 64; further ones are closed |
| Stalled connection | closed after 30 seconds |

## No model calls

The client summary uses the Korean catalog. Its phrases are machine-translated and unreviewed, so the visitor must confirm them before approving. The app won't start if a phrase has no text, and if one were ever missing at run time the Korean summary is held back and the English stays available. Hosted mode has no path to the model client, with or without a key in the environment. The image contains no recordings, statements or answer key.

## Link the direct URL

Link the app's own address. The app sends `frame-ancestors 'none'`, so it won't load inside another site's iframe, and that includes the frame on a Hugging Face Space page. Making it work embedded would need a `SameSite=None` cookie, which Safari and other browsers that block third-party cookies would drop, and it would allow other sites to frame a page with working buttons.

Arriving by a link from another site works and keeps the visitor's session. The first page load carries no cookie, and the requests that page makes do.

## Publish on Hugging Face

Hugging Face's docs say Docker Spaces need a paid plan on personal accounts, while CPU Basic hardware itself has no hourly cost. Check your plan first. This Space needs no secrets or variables: Hugging Face sets `SPACE_HOST`, and the app reads it.

Run these from a checkout of this repository, after the hosted demo has merged. Replace `<user>` with your Hugging Face name.

```bash
# 1. Sign in. This asks for a token with write access from huggingface.co/settings/tokens.
uvx --from huggingface_hub hf auth login

# 2. Create the Space.
uvx --from huggingface_hub hf repos create <user>/fbar-worksheet-demo --type space --space-sdk docker --public

# 3. Build the upload from main: only what the image needs, plus the Space's README.
git fetch origin
BUNDLE=$(mktemp -d)
git archive origin/main Dockerfile .dockerignore LICENSE pyproject.toml uv.lock .python-version \
  src results/muse-spark-1.3_synth.json | tar -x -C "$BUNDLE"
git show origin/main:deploy/huggingface/README.md > "$BUNDLE/README.md"

# 4. Upload. Hugging Face builds the image from it, which takes a few minutes.
uvx --from huggingface_hub hf upload <user>/fbar-worksheet-demo "$BUNDLE" . --type space \
  --commit-message "Publish the hosted demo"
```

The direct URL is `https://<user>-fbar-worksheet-demo.hf.space`, in lower case. The Space's "Embed this Space" menu shows the exact address.

Check these at the direct URL before sharing it:

1. The page loads with six rows and an aggregate of $373,618.
2. Acknowledge one item and reload. The acknowledgement is still there.
3. Open the same URL in a private window. It shows no acknowledgements.

If the Space's logs show `refused GET` lines with a different `host=`, for every request and not just the odd probe, then the proxy is rewriting the Host header. Don't try to work around that with a different public host, because the Origin check depends on it too. The hosted mode would need a change for that host.

Spaces on free hardware sleep after a period with no visitors, and waking one takes a little while. Sleeping and restarting clear all worksheets.
