# Drama Clip Scout

Drama Clip Scout is a local Docker application for finding, ranking, reviewing, and optionally downloading public streamer clip leads. It collects public metadata and links from Reddit, X/Twitter, and Kiwi Farms, stores them in SQLite, and exposes both a browser dashboard and a FastAPI API that Hermes can call.

Drama Clip Scout is a research aid, not a fact-checker. Every result is a lead to review against its linked source before making a claim.

Hermes integration is optional. The dashboard, API, collectors, reports, and downloader all work as a standalone local application.

## What It Does

- Collects Reddit posts through public web pages or the Reddit API.
- Discovers X/Twitter status URLs through free web search, direct URLs, Reddit links, the X API, or an official X archive.
- Searches public Kiwi Farms results through the configured Kiwifarms Bridge, with a legacy direct-search fallback.
- Extracts supported media links from public source metadata.
- Distinguishes verified video/media results from photos and unverified status leads.
- Ranks leads from 0–100 using engagement, recency, discussion activity, keywords, media, and configured streamer names.
- Filters by source, time window, score, keywords, X account, and verified-video status.
- Generates browser and Markdown reports.
- Copies links and ready-to-run `yt-dlp` commands.
- Downloads mixed batches of pasted X/Twitter, YouTube, Reddit, Instagram, and Twitch media links without requiring prior collection.
- Optionally downloads media into the local `data/downloads/` directory.
- Gives Hermes a structured `/agent/search-clips` endpoint.

## Safety and Scope

- The web service binds only to `127.0.0.1:8787`.
- The container runs as a non-root user with `no-new-privileges`.
- Collection stores links and metadata; it does not download videos unless you press a download button or call the download endpoint.
- Unverified X search results are not labeled as videos.
- Common email, phone, street-address, family-name, and IP-address patterns are redacted from stored Kiwi Farms snippets.
- Kiwi Farms collection does not log in, solve CAPTCHAs, or bypass access controls.
- Setup, update, stop, and container-reset scripts do not delete Hermes data.
- Never delete `~/.hermes` for this project. That directory belongs to Hermes and may contain its configuration, memory, sessions, skills, and API keys.

## Requirements

- Docker Desktop or another Docker installation with Docker Compose.
- A checkout of this repository.
- Optional: an existing Docker container named `hermes` for Hermes integration.
- Optional: Reddit and X API credentials for their official API paths.

The public web-search paths work without paid search credentials, although source sites may limit what they expose to logged-out requests.

## Quick Start

From the repository directory:

```bash
./setup.sh
./start.sh
```

`setup.sh`:

1. Creates the external Docker network `drama-net` if necessary.
2. Connects an existing container named `hermes` to that network.
3. Creates `.env` from `.env.example` only when `.env` is missing.
4. Creates the local `data/` directory.
5. Leaves existing `.env`, Hermes, and `~/.hermes` data unchanged.

After startup, open:

- [Drama Clip Scout dashboard](http://127.0.0.1:8787/ui)
- [FastAPI documentation](http://127.0.0.1:8787/docs)
- [Hermes dashboard](http://127.0.0.1:9119), when Hermes is running

Verify that the API is ready:

```bash
curl -sS http://127.0.0.1:8787/health
```

The response should contain `"status": "ok"`. Source-specific `configured` values report whether optional credentials or bridge settings are available; they do not prevent the app from starting.

`./setup.sh` is normally needed only once. Use `./start.sh` to build and start the app again later, and `./stop.sh` to stop it without removing stored data.

## URLs and Docker Networking

| Purpose | URL |
| --- | --- |
| Drama Clip Scout dashboard | `http://127.0.0.1:8787/ui` |
| Drama Clip Scout API | `http://127.0.0.1:8787` |
| FastAPI docs | `http://127.0.0.1:8787/docs` |
| Hermes dashboard on the host | `http://127.0.0.1:9119` |
| Hermes Gateway on the host | `http://127.0.0.1:8642` |
| Drama Clip Scout from Hermes | `http://drama-clip-scout:8787` |
| Hermes Gateway from Drama Clip Scout | `http://hermes:8642` |

Inside the Hermes container, do not use `127.0.0.1:8787` for Drama Clip Scout. Container-local `127.0.0.1` points back to Hermes. Use:

```text
http://drama-clip-scout:8787/agent/search-clips
```

The Docker Compose service mounts `./data` on the host to `/data` in the container and joins the external `drama-net` network.

## Dashboard Guide

The main dashboard combines collection and filtered search. Enter a prompt, optionally enter a person/topic, choose a target, and press `Collect + Find`.

### Target Options

| Dashboard target | API source value | Sources included |
| --- | --- | --- |
| Reddit | `reddit` | Reddit only |
| X / Twitter | `x` | X only |
| Reddit + X | `reddit_x` | Reddit and X; skips Kiwi Farms |
| Kiwi Farms | `kiwifarms` | Kiwi Farms only |
| All Sources | `all` | Reddit, X, and Kiwi Farms |

`All Sources` and `Kiwi Farms` need either a person/topic or a non-empty prompt so the Kiwi Farms collector has a search query.

When an X account is supplied with `Reddit + X` or `All Sources`, the account restriction applies to X results while Reddit results remain eligible.

### Limit and Search Modes

The dashboard limit has a minimum and default of 25.

| Behavior | Standard mode | Deep search |
| --- | --- | --- |
| Default result/collection limit | 25 | 25 |
| Limits above 25 | Capped at 25 | Allowed up to each endpoint’s maximum |
| Reddit top comments | Skipped | Up to 5 per collected post |
| Kiwi Farms request budget | Up to 5 | Up to 25 |
| Default time window after enabling | Day | Month |

Turning on `Deep search / more results` changes the currently selected default `day` window to `month`. You can choose another time window afterward.

Most network collection and agent-search endpoints allow up to 100 items. Official X archive import allows up to 5,000.

### Videos Only

`Videos only` returns results with verified video/media evidence.

It excludes:

- X photo posts.
- X status links found by search when X did not expose enough metadata to verify video.
- Text-only Kiwi Farms results.
- Other items not marked as video by their source collector.

Clear the checkbox when you also want unverified status leads and text-only source leads.

### Dashboard Actions

- `Collect + Find`: collects selected sources, then searches ranked results.
- `Collect Only`: collects without running the final ranked search.
- `Copy Hermes Prompt`: copies a Hermes-ready handoff containing the current filters.
- `Copy URLs`: copies primary URLs for the displayed results.
- `Copy yt-dlp Command`: copies downloadable media URLs as a Docker command.
- `Download all shown`: downloads every displayed result currently marked as downloadable.
- Per-card `Download`: downloads one result.

Collection failures are isolated by source. A Kiwi Farms outage or one dead X status URL does not discard successful results from the other selected sources or URLs.

### Multi-link Downloader

The dashboard includes one `Multi-link Downloader` panel. Paste up to 100 mixed X/Twitter, YouTube, Reddit, Instagram, Twitch, Kick, and Rumble links, one per line, then press `Download links`.

The batch downloader:

- Accepts direct X/Twitter status links, including video, photo, and text-only posts.
- Saves X photos as their original image files.
- Saves a PNG tweet-card screenshot when an X post has no downloadable video. Photo posts receive both the original photo and the screenshot.
- Accepts YouTube watch, Shorts, live, embed, and `youtu.be` links.
- Accepts Reddit post, `redd.it`, and `v.redd.it` links.
- Saves the original image from photo-only Instagram posts while continuing to download Instagram videos normally.
- Accepts Twitch clips, VODs, and live channel links. A live channel must currently be streaming.
- Accepts Kick clips, VODs, and live channel links. A live channel must currently be streaming.
- Accepts Rumble video, embed, and livestream links.
- Normalizes alternate link formats and removes duplicate videos or posts.
- Runs up to two downloads concurrently.
- Reports success or failure for every unique link.
- Saves every platform’s files together under `./data/downloads/link-downloader/`.
- Shows each exact filename and path after completion. Use `Save file` to copy an output into the browser's Downloads folder.
- Replaces the matching saved output on retry so the file and modification time are refreshed.
- Names files from the media title and appends the platform extractor and video ID to prevent same-title collisions, for example `Video_topic [Youtube-abc123].mp4`.

## User Interface Pages

| Page | URL |
| --- | --- |
| Dashboard | `http://127.0.0.1:8787/ui` |
| Clip browser | `http://127.0.0.1:8787/ui/clips` |
| Collection runs | `http://127.0.0.1:8787/ui/runs` |
| Readable report | `http://127.0.0.1:8787/ui/report` |
| Settings status | `http://127.0.0.1:8787/ui/settings` |
| Markdown report | `http://127.0.0.1:8787/reports/latest.md` |

The settings page reports values as `configured` or `missing`; it does not display credential values.

## Configuration

Run `./setup.sh` or copy the example manually:

```bash
cp .env.example .env
```

Then edit `.env`. Do not commit real credentials.

| Variable | Purpose | Default/example |
| --- | --- | --- |
| `REDDIT_CLIENT_ID` | Reddit script-app client ID | empty |
| `REDDIT_CLIENT_SECRET` | Reddit script-app secret | empty |
| `REDDIT_USERNAME` | Reddit account for script authentication | empty |
| `REDDIT_PASSWORD` | Reddit account password for script authentication | empty |
| `REDDIT_USER_AGENT` | Descriptive Reddit user agent | `drama-clip-scout/0.1 by YOUR_REDDIT_USERNAME` |
| `X_BEARER_TOKEN` | X API v2 bearer token | empty |
| `X_TARGET_ACCOUNTS` | Comma-separated default X accounts | empty |
| `KIWIFARMS_BRIDGE_URL` | Private Kiwifarms Bridge base URL | set in the ignored `.env`; no committed default |
| `KIWIFARMS_BRIDGE_TIMEOUT_SECONDS` | Bridge request timeout | `75` |
| `KIWIFARMS_BASE_URL` | Legacy direct-search base URL | `https://kiwifarms.st` |
| `KIWIFARMS_FALLBACK_BASE_URLS` | Comma-separated legacy fallback URLs | empty |
| `KIWIFARMS_REQUEST_DELAY_SECONDS` | Delay between legacy direct requests | `1.5` |
| `KIWIFARMS_MAX_PAGES` | Default Kiwi Farms request budget | `10` |
| `USER_AGENT` | User agent for public web requests | browser-style default |
| `OUTBOUND_PROXY_URL` | Optional proxy for outbound public requests | empty |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:////data/clips.db` |
| `DEFAULT_SUBREDDIT` | Dashboard’s initial subreddit | `LivestreamFail` |
| `API_HOST` | Container listen address | `0.0.0.0` |
| `API_PORT` | Container API port | `8787` |
| `HERMES_GATEWAY_INTERNAL_URL` | Hermes Gateway address inside Docker | `http://hermes:8642` |
| `KNOWN_STREAMER_NAMES` | Optional comma-separated names that add ranking signals | empty |

### Reddit Credentials

Reddit credentials are optional for public-web collection. To configure the official API:

1. Open [Reddit app preferences](https://www.reddit.com/prefs/apps).
2. Create a `script` app.
3. Use `http://127.0.0.1:8787` as the redirect URI if Reddit requests one.
4. Add the client ID, secret, username, password, and a descriptive user agent to `.env`.

The API request field `source_mode` accepts:

- `auto`: use the Reddit API when fully configured, otherwise use public web pages.
- `api`: require the Reddit API configuration.
- `web`: use public Reddit pages.

The dashboard explicitly uses the public web path.

### X/Twitter Credentials and Collection Paths

An X bearer token is optional. If configured, `/collect/x` with `source_mode: "auto"` can use X API v2. The dashboard primarily uses free public discovery and direct public status pages.

Supported X paths:

1. Free web search for public status URLs about a person or topic.
2. Direct `x.com/.../status/...` or `twitter.com/.../status/...` URLs.
3. X status URLs found in collected Reddit posts and comments.
4. X API v2 recent search when a bearer token is configured.
5. Import from an official X archive owned by the user.
6. Archive/search-page discovery through the archive-search endpoint.

Free discovery tries public search pages and stores normalized direct status URLs. The collector then attempts to read metadata from each public status page. One failed or deleted status is skipped while remaining URLs continue.

If public metadata exposes an X video-thumbnail marker, the result is classified as video. Generic X media images are classified as photos. If metadata cannot be fetched, the status remains an unverified lead and is excluded by `Videos only`.

For official archive import, place the extracted archive under the repository’s `data/` directory. A host path such as:

```text
./data/x-archive/data/tweets.js
```

is visible inside the container as:

```text
/data/x-archive/data/tweets.js
```

You can provide the file, `/data/x-archive`, or `/data/x-archive/data`; the importer looks for `tweets*.js` and `tweets*.json`.

### Kiwi Farms Public Search

Kiwi Farms does not require credentials in this application. The preferred path uses the standalone [Kiwifarms Bridge](https://github.com/ariqserazi/kiwifarm-Bridge), which performs the ordinary public guest-search flow and returns structured, redacted JSON.

The collector:

- Searches in batches of at most 20.
- Uses only verified IDs and URLs returned by the source.
- Optionally fetches verified thread details when the request budget permits.
- Stores one stable lead per verified result/thread.
- Extracts supported X, YouTube, Twitch, Reddit, Streamable, TikTok, and direct-video links.
- Does not recursively crawl outbound media sites.

`KIWIFARMS_MAX_PAGES` is the default bridge request budget, including search and optional enrichment requests. The dashboard explicitly uses up to 5 requests in standard mode and up to 25 in deep mode.

Set `KIWIFARMS_BRIDGE_URL=` to disable the bridge and use the legacy direct public-search client with `KIWIFARMS_BASE_URL` and optional `KIWIFARMS_FALLBACK_BASE_URLS`.

A bridge outage, guest-access error, 403, 429, CAPTCHA, or authentication requirement produces a nonfatal collection result. Reddit and X collection continue.

## Operations

| Command | Effect |
| --- | --- |
| `./setup.sh` | Creates the network, optional Hermes connection, `.env`, and `data/` |
| `./start.sh` | Builds and starts only `drama-clip-scout` |
| `./update.sh` | Rebuilds and force-recreates only `drama-clip-scout` |
| `./stop.sh` | Stops only `drama-clip-scout` |
| `./logs.sh` | Follows the app container logs |
| `./reset-container.sh` | Removes only the app container; preserves SQLite and Hermes |
| `./collect_reddit.sh` | Calls `/collect/reddit` with default API values |
| `./collect_x.sh` | Calls `/collect/x` with default API values |
| `./collect_all.sh` | Calls `/collect/all`; Kiwi Farms is skipped without a query payload |

## API Reference

Interactive schemas and request forms are available at [http://127.0.0.1:8787/docs](http://127.0.0.1:8787/docs).

### Read and Search Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Service and navigation links |
| `GET` | `/health` | Service and source-configuration status |
| `GET` | `/clips` | Filtered ranked results |
| `GET` | `/clips/{item_id}` | One result with comments and raw metadata |
| `GET` | `/sources` | Stored source records |
| `GET` | `/runs` | Recent collection runs |
| `GET` | `/reports/latest.md` | Markdown report |
| `POST` | `/agent/search-clips` | Structured Hermes-facing search |
| `POST` | `/rank` | Re-rank all non-removed items |

### Collection Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/collect/reddit` | Reddit API or public-web collection |
| `POST` | `/collect/x` | X API or public-page collection |
| `POST` | `/collect/kiwifarms` | Public Kiwi Farms query |
| `POST` | `/collect/all` | Reddit, X, and optional Kiwi Farms collection |
| `POST` | `/collect/x/from-web-search` | Free public search discovery |
| `POST` | `/collect/x/from-google-search` | Compatibility alias for free web search |
| `POST` | `/collect/x/from-reddit` | Discover X status URLs in collected Reddit data |
| `POST` | `/collect/x/from-archive-search` | Search/archive-page discovery |
| `POST` | `/collect/x/archive` | Import an official X archive |

### Download Endpoint

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/downloads/files/{file_path}` | Save a previously downloaded file through the browser |
| `POST` | `/downloads/items/{item_id}` | Download one item’s media with `yt-dlp` |
| `POST` | `/downloads/links` | Download up to 100 mixed X/Twitter, YouTube, Reddit, Instagram, Twitch, Kick, and Rumble links |
| `POST` | `/downloads/x-links` | Compatibility alias for the unified link downloader |

### Shared Search Values

Source values:

- `reddit`
- `x`
- `reddit_x`
- `kiwifarms`
- `all`

Time-window values:

- `day`: last 24 hours
- `week`: last 7 days
- `month`: last 30 days
- `year`: since January 1 of the current year
- `all`: no created-time cutoff

Items without a known created time remain eligible for bounded time windows.

## API Examples

### Health

```bash
curl -sS http://127.0.0.1:8787/health
```

### Collect Reddit

```bash
curl -sS -X POST http://127.0.0.1:8787/collect/reddit \
  -H 'Content-Type: application/json' \
  -d '{
    "subreddit": "LivestreamFail",
    "mode": "hot",
    "source_mode": "web",
    "limit": 25,
    "top_comments_limit": 0
  }'
```

Reddit modes are `hot`, `new`, `rising`, `top_day`, and `top_week`.

### Discover X Statuses by Topic

```bash
curl -sS -X POST http://127.0.0.1:8787/collect/x/from-web-search \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "Streamer University",
    "search_provider": "web",
    "limit": 25
  }'
```

Optionally include `"account": "Awk20000"` to restrict discovery to one X account.

### Collect Direct X URLs

```bash
curl -sS -X POST http://127.0.0.1:8787/collect/x \
  -H 'Content-Type: application/json' \
  -d '{
    "source_mode": "web",
    "urls": [
      "https://x.com/example/status/1234567890"
    ],
    "limit": 25
  }'
```

### Import an Official X Archive

```bash
curl -sS -X POST http://127.0.0.1:8787/collect/x/archive \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/data/x-archive",
    "account": "your_handle",
    "limit": 500
  }'
```

### Collect Kiwi Farms

```bash
curl -sS -X POST http://127.0.0.1:8787/collect/kiwifarms \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "Streamer University",
    "limit": 25,
    "max_pages": 5
  }'
```

### Collect All Sources

```bash
curl -sS -X POST http://127.0.0.1:8787/collect/all \
  -H 'Content-Type: application/json' \
  -d '{
    "reddit": {
      "subreddit": "LivestreamFail",
      "source_mode": "web",
      "limit": 25,
      "top_comments_limit": 0
    },
    "x": {
      "source_mode": "web",
      "limit": 25
    },
    "kiwifarms": {
      "query": "Streamer University",
      "limit": 25,
      "max_pages": 5
    }
  }'
```

Each source returns its own result object. A source failure does not discard the other source results.

### Search Reddit and X Only

```bash
curl -sS -X POST http://127.0.0.1:8787/agent/search-clips \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "reddit_x",
    "time_window": "month",
    "keywords": ["Streamer", "University"],
    "min_drama_score": 0,
    "has_video": true,
    "limit": 25
  }'
```

This searches stored Reddit and X results and excludes Kiwi Farms. Collection and search are separate API operations; the dashboard’s `Collect + Find` button performs both.

### Filter the Clip API

```bash
curl -sS \
  'http://127.0.0.1:8787/clips?source=reddit_x&time_window=month&has_video=true&keyword=university&limit=25'
```

### Generate a Filtered Report

```bash
curl -sS \
  'http://127.0.0.1:8787/reports/latest.md?source=reddit_x'
```

## Hermes Integration

Hermes should call:

```text
http://drama-clip-scout:8787/agent/search-clips
```

Example Hermes request:

```text
Use the local Drama Clip Scout API at http://drama-clip-scout:8787/agent/search-clips.
Search Reddit and X only for verified videos about Streamer University from the last month.
Use source "reddit_x", time_window "month", keywords ["Streamer", "University"],
min_drama_score 0, has_video true, and limit 25.
Return direct links, titles, source, score, and a short reason.
Treat every result as a lead and verify the linked source before making a claim.
```

The dashboard’s `Copy Hermes Prompt` button generates a handoff from the current controls.

## Downloads

The Docker image includes:

- `yt-dlp`
- `ffmpeg`
- Deno as the JavaScript runtime used by current `yt-dlp` extractors

Downloads are stored under:

```text
/data/downloads/<source>/<item-id>-<title-slug>
```

On the host, that maps to:

```text
./data/downloads/<source>/<item-id>-<title-slug>
```

For Kiwi Farms items, the downloader attempts the primary media URL plus every attached supported media URL. The result may be:

- `success`: every attempted media URL succeeded.
- `partial`: at least one URL succeeded and at least one failed.
- `failed`: no URL succeeded.

Example:

```bash
curl -sS -X POST http://127.0.0.1:8787/downloads/items/123
```

Download a mixed set of X/Twitter, YouTube, Reddit, Instagram, Twitch, Kick, and Rumble links:

```bash
curl -sS -X POST http://127.0.0.1:8787/downloads/links \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": [
      "https://x.com/example/status/1234567890",
      "https://www.youtube.com/watch?v=abcdefghijk",
      "https://www.reddit.com/r/videos/comments/abc123/example/",
      "https://www.instagram.com/reel/DbRJmS-pUBT/",
      "https://clips.twitch.tv/ExampleClipSlug",
      "https://kick.com/example/clips/clip_01J8RGZRKHXHXXKJEHGRM932A5",
      "https://rumble.com/v6abcde-example-video.html"
    ]
  }'
```

All unified-link downloads are stored in the same directory:

```text
./data/downloads/link-downloader/
```

Video filenames start with the video or post title and end with a platform extractor plus video ID. X photo and screenshot filenames include the account handle and status ID to avoid collisions.

Or run `yt-dlp` directly:

```bash
docker exec drama-clip-scout \
  yt-dlp -P /data/downloads \
  "https://example.com/video-or-post-url"
```

X/Twitter, YouTube, Reddit, Instagram, Twitch, Kick, or Rumble downloads may still fail when the source blocks logged-out access, requires cookies, removes the post, or restricts the media. Twitch and Kick channel links also fail when the channel is offline. X screenshots require the post text to be available either in the local collection database or in the public status-page metadata.

## Storage and Ranking

SQLite is stored at:

```text
./data/clips.db
```

The database contains:

- Source definitions.
- Collected Reddit, X, and Kiwi Farms items.
- Comments and source metrics.
- Media metadata and variants.
- Collection-run history.
- A history of ranking records.

Ranking signals include:

- Engagement score, likes, or reposts.
- Comment/reply volume and velocity.
- Views where available.
- Recency.
- Drama/reaction keywords.
- Verified video or media metadata.
- Multiple independent media links.
- Query-match frequency.
- Configured streamer-name matches.
- Intensity terms in collected top comments.

Labels are:

- `high potential`: score 70 or above.
- `medium potential`: score 40–69.99.
- `low potential`: below 40.

Rankings are heuristics. They do not confirm that a claim is true or that a clip has sufficient context.

## Troubleshooting

### The dashboard returns no recent results

Check the time window. `day` means the last 24 hours, and an event from several days ago will not appear. Enabling deep search changes the default day window to month, or you can select `week`, `month`, `year`, or `all`.

### Videos Only returns fewer results

This is expected when X exposes only a photo or does not expose media metadata. Clear `Videos only` to review unverified status links and text-only leads.

### One X URL returns 404

The collector skips that URL and continues processing the rest of the batch. The collection result reports the skipped URL.

### X API errors with 401, 402, 403, or 429

- `401`: verify the bearer token.
- `402`: the X project may require paid usage credits.
- `403`: the app/token may not have access to that endpoint.
- `429`: wait for the rate-limit window or use a smaller request.

You can continue using the free public discovery paths without X API access.

### Kiwi Farms is unavailable

The bridge or public guest search may be temporarily unavailable. Review the nonfatal note in the dashboard or `/runs`, then retry later. Reddit and X collection continue independently.

### A download fails

Open the source URL first to confirm it is still public. Source sites may require authentication, cookies, or formats unsupported by the current extractor. Check:

```bash
./logs.sh
```

### The app cannot start because `drama-net` is missing

Run:

```bash
./setup.sh
./start.sh
```

## Local Development

Docker is the recommended way to run the complete application because the image includes `ffmpeg`, Deno, and `yt-dlp`. For API development and tests, you can also use Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
mkdir -p data
DATABASE_URL=sqlite:///./data/clips.db \
  uvicorn app.main:app --reload --host 127.0.0.1 --port 8787
```

When running directly on the host, install `ffmpeg` and Deno separately if you need the same download support as the Docker image.

### Project Layout

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI application setup and shared UI styles |
| `app/routers/` | Dashboard, collection, search, report, and download endpoints |
| `app/reddit_client.py` | Reddit API and public-web collection |
| `app/x_client.py` | X API, public-page, web-search, and archive collection |
| `app/kiwifarms_client.py` | Bridge and legacy public-search collection |
| `app/ranking.py` | Lead scoring and potential labels |
| `app/models.py` | SQLAlchemy database models |
| `tests/` | Unit tests and saved HTML fixtures |
| `data/` | Persistent SQLite data and downloaded files; not baked into the image |

## Tests

The tests use in-memory SQLite databases, mocks, and saved minimal fixtures. They do not perform live Kiwi Farms collection.

With the Python dependencies installed:

```bash
python3 -m unittest discover -s tests -v
```

If `pytest` is installed:

```bash
python3 -m pytest -q
```

## Reset and Data Removal

Remove only the application container:

```bash
./reset-container.sh
```

This preserves `./data/clips.db`, downloads, Hermes, and `~/.hermes`.

To intentionally delete only Drama Clip Scout’s SQLite database:

```bash
rm -f ./data/clips.db ./data/clips.db-shm ./data/clips.db-wal
```

Downloaded files under `./data/downloads/` are separate and are not removed by that command.

Do not delete `~/.hermes`; it belongs to Hermes.
