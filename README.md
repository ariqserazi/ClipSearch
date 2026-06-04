# Drama Clip Scout

Drama Clip Scout is a separate local Docker app that collects public links and metadata for possible streamer drama clip leads. It uses the official Reddit API and official X API, ranks results by potential usefulness, and exposes a local FastAPI API that Hermes can call.

It does not replace Hermes. Your existing Hermes Docker setup stays in `hermes-docker`, keeps using its own container named `hermes`, and keeps its own data in `~/.hermes`.

## Two Browser UIs

Hermes Dashboard:

http://127.0.0.1:9119

Drama Clip Scout UI:

http://127.0.0.1:8787/ui

Hermes is where you ask the agent questions. Drama Clip Scout UI is where you manually collect, browse, filter, and review ranked clip leads.

FastAPI docs:

http://127.0.0.1:8787/docs

## Docker Networking

Drama Clip Scout uses an external Docker network named `drama-net`.

The setup script:

1. Creates `drama-net` if it does not exist.
2. Checks whether a Docker container named `hermes` exists.
3. Connects `hermes` to `drama-net` if it is not already connected.
4. Leaves Hermes data, config, memory, sessions, skills, and API keys alone.

Hermes should call Drama Clip Scout from inside Docker at:

http://drama-clip-scout:8787/agent/search-clips

Do not use `http://127.0.0.1:8787` from inside the Hermes container. Inside Hermes, `127.0.0.1` points back to the Hermes container itself.

If Drama Clip Scout ever needs to call Hermes Gateway from inside Docker, it can use:

http://hermes:8642

On your Mac, Drama Clip Scout is bound only to:

http://127.0.0.1:8787

This keeps the service local instead of exposing it publicly.

## API Credentials

Drama Clip Scout does not hardcode API keys and does not create a real `.env` with secrets for you.

Copy the example file:

```bash
cp .env.example .env
```

Then edit `.env` manually and add your credentials.

### Reddit API Credentials

1. Go to https://www.reddit.com/prefs/apps.
2. Click `create another app` or `create app`.
3. Choose `script`.
4. Set a name such as `drama-clip-scout`.
5. Use `http://127.0.0.1:8787` as the redirect URI if Reddit asks for one.
6. Copy the client ID and client secret into `.env`.
7. Add your Reddit username and password.
8. Set a clear user agent, for example:

```env
REDDIT_USER_AGENT=drama-clip-scout/0.1 by your_reddit_username
```

### X API Credentials

1. Go to https://developer.x.com/.
2. Create or use an existing developer project/app.
3. Generate a bearer token for X API v2.
4. Put it in `.env` as `X_BEARER_TOKEN`.
5. Optionally set comma-separated target accounts:

```env
X_TARGET_ACCOUNTS=streamer_one,streamer_two
```

X collection is optional because X API access may require a paid tier. If no X token is configured, X collection will be skipped with a clear message.

## Setup

From this folder:

```bash
./setup.sh
```

This creates the Docker network, connects the existing `hermes` container if it exists, creates `.env` only if missing, and creates `data/`.

It never deletes `~/.hermes`. Deleting `~/.hermes` would delete Hermes data and should not be done for this project.

## Start

```bash
./start.sh
```

Then open:

http://127.0.0.1:8787/ui

## Stop

```bash
./stop.sh
```

This stops only `drama-clip-scout`. It does not stop Hermes and does not delete data.

## Logs

```bash
./logs.sh
```

## Collect Data

Collect Reddit posts:

```bash
./collect_reddit.sh
```

Collect X posts:

```bash
./collect_x.sh
```

Collect all sources:

```bash
./collect_all.sh
```

You can also use the buttons in:

http://127.0.0.1:8787/ui

## UI Pages

Dashboard:

http://127.0.0.1:8787/ui

Clip browser:

http://127.0.0.1:8787/ui/clips

Runs:

http://127.0.0.1:8787/ui/runs

Settings status:

http://127.0.0.1:8787/ui/settings

Readable report:

http://127.0.0.1:8787/ui/report

Markdown report:

http://127.0.0.1:8787/reports/latest.md

The settings page only says `configured` or `missing`. It does not show API key values and does not send secret values to the browser.

## Hermes Prompt

Use this in Hermes:

```text
Use the local Drama Clip Scout API at http://drama-clip-scout:8787/agent/search-clips. Find the top high potential r/LivestreamFail clips from the last day. Return titles, links, scores, and why each one may be useful. Do not claim anything is confirmed drama unless the source clearly supports it.
```

## API Examples

Health:

```bash
curl http://127.0.0.1:8787/health
```

Reddit collection:

```bash
curl -X POST http://127.0.0.1:8787/collect/reddit \
  -H 'Content-Type: application/json' \
  -d '{"mode":"hot","limit":25}'
```

X collection:

```bash
curl -X POST http://127.0.0.1:8787/collect/x \
  -H 'Content-Type: application/json' \
  -d '{"limit":25}'
```

Hermes-facing search endpoint:

```bash
curl -X POST http://127.0.0.1:8787/agent/search-clips \
  -H 'Content-Type: application/json' \
  -d '{"source":"all","time_window":"day","keywords":[],"min_drama_score":70,"limit":20}'
```

Inside Docker, Hermes should use:

```text
http://drama-clip-scout:8787/agent/search-clips
```

## What Gets Stored

Drama Clip Scout stores links and metadata only:

- Reddit post IDs, titles, authors, scores, comments, links, thumbnails, and raw JSON.
- X post IDs, text, author metadata, public metrics, media metadata, links, and raw JSON.
- Ranking scores and short reasoning.

It does not download videos by default, does not repost videos, and does not include video downloading buttons.


## Reset Only Drama Clip Scout

Remove only the Drama Clip Scout container:

```bash
./reset-container.sh
```

This does not delete SQLite data.

Fully reset only Drama Clip Scout data with this clearly destructive command:

```bash
rm -f ./data/clips.db ./data/clips.db-shm ./data/clips.db-wal
```

Do not delete `~/.hermes` for this project. That belongs to Hermes and may contain Hermes config, memory, sessions, skills, and API keys.
