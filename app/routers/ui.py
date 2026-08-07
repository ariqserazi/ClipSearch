import html
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Item, Ranking, Run, Source
from app.routers.clips import latest_ranking_subquery, query_ranked_items, serialize_item
from app.web_headers import DEFAULT_HEADERS, outbound_proxy_url

router = APIRouter()


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Drama Clip Scout</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="stylesheet" href="/ui.css">
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/ui">Drama Clip Scout</a>
      <nav>
        <a href="/ui">Dashboard</a>
        <a href="/ui/clips">Clips</a>
        <a href="/ui/runs">Runs</a>
        <a href="/ui/report">Report</a>
        <a href="/ui/settings">Settings</a>
        <a href="/docs">API Docs</a>
      </nav>
    </header>
    {body}
  </main>
</body>
</html>"""
    )


def _source_counts(db: Session) -> dict[str, int]:
    rows = db.query(Source.name, func.count(Item.id)).join(Item, Item.source_id == Source.id).group_by(Source.name).all()
    return {name: count for name, count in rows}


def _download_controls_script() -> str:
    return """
<script>
function setDownloadMessage(text, kind = "") {
  const message = document.getElementById("message");
  if (!message) return;
  message.hidden = false;
  message.className = "message" + (kind ? " " + kind : "");
  message.textContent = text;
}

function staticDownloadSummary(data) {
  if (data.status === "success") {
    const count = (data.files || []).length;
    return `Downloaded ${count} file${count === 1 ? "" : "s"} to ${data.host_dir || data.download_dir}.`;
  }
  if (data.status === "partial") {
    const count = (data.files || []).length;
    return `Downloaded ${count} file${count === 1 ? "" : "s"}; ${data.media_failed || 0} of ${data.media_count || 0} media URLs failed. Files are in ${data.host_dir || data.download_dir}.`;
  }
  const detail = (data.stderr_tail || data.stdout_tail || "yt-dlp could not resolve this URL.").trim();
  return `Download failed for item #${data.item_id}: ${detail}`;
}

async function downloadStaticItem(itemId, button, showResult = true) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Downloading...";
  try {
    const response = await fetch(`/downloads/items/${itemId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || "Download failed");
    const succeeded = data.status !== "failed";
    button.textContent = data.status === "success" ? "Downloaded" : data.status === "partial" ? "Partial" : "Retry";
    if (showResult) setDownloadMessage(staticDownloadSummary(data), succeeded ? "success" : "error");
    return { succeeded, fileCount: (data.files || []).length, failedMedia: data.media_failed || 0 };
  } catch (error) {
    button.textContent = "Retry";
    if (showResult) setDownloadMessage(error.message, "error");
    return { succeeded: false, fileCount: 0, failedMedia: 0 };
  } finally {
    button.disabled = false;
    if (button.textContent === "Downloading...") button.textContent = originalText;
  }
}

async function downloadAllStaticItems(button) {
  const itemButtons = [...document.querySelectorAll("[data-download-id]")];
  const uniqueButtons = [...new Map(itemButtons.map((itemButton) => [itemButton.dataset.downloadId, itemButton])).values()];
  if (!uniqueButtons.length) {
    setDownloadMessage("No clips are shown to download.", "error");
    return;
  }
  const originalText = button.textContent;
  let nextIndex = 0;
  let completed = 0;
  let succeeded = 0;
  let fileCount = 0;
  let failedMedia = 0;
  button.disabled = true;
  const worker = async () => {
    while (nextIndex < uniqueButtons.length) {
      const itemButton = uniqueButtons[nextIndex++];
      const result = await downloadStaticItem(itemButton.dataset.downloadId, itemButton, false);
      completed += 1;
      succeeded += result.succeeded ? 1 : 0;
      fileCount += result.fileCount;
      failedMedia += result.failedMedia;
      button.textContent = `Downloading ${completed}/${uniqueButtons.length}`;
      setDownloadMessage(`Downloading ${completed} of ${uniqueButtons.length} shown clips...`);
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(2, uniqueButtons.length) }, worker));
    const failed = uniqueButtons.length - succeeded;
    const summary = `Finished: ${succeeded} of ${uniqueButtons.length} posts downloaded (${fileCount} file${fileCount === 1 ? "" : "s"}) to data/downloads.${failedMedia ? ` ${failedMedia} individual download attempts failed.` : ""}${failed ? ` ${failed} items failed; use Retry on those cards.` : ""}`;
    setDownloadMessage(summary, failed ? "error" : "success");
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function downloadSinglePinterestPin(pinId, button) {
  const pin = pinterestPinsCache.find((p) => String(p.pin_id) === String(pinId));
  if (!pin) return;
  button.disabled = true;
  button.textContent = "Downloading...";
  try {
    const data = await postJson("/research/pinterest-download", { query: pinterestQueryCache, pins: [pin] });
    const download = (data.downloads || [])[0];
    const card = button.closest(".pinterest-card");
    if (download && download.status === "success" && download.file && card) {
      const label = download.title || download.description || `Pinterest pin ${download.pin_id}`;
      card.innerHTML = `<img src="${esc(download.file.download_url)}?inline=true" alt="${esc(label)}">
        <p>${esc(label)}</p>
        <p class="muted">${download.pinner ? `@${esc(download.pinner)} • ` : ""}${esc(download.width || "?")}×${esc(download.height || "?")}</p>
        <div class="actions small"><a class="button ghost small-button" href="${esc(download.pin_url)}" target="_blank" rel="noreferrer">Open pin</a><a class="button ghost small-button" href="${esc(download.file.download_url)}" download>Save file</a></div>`;
    } else {
      button.disabled = false;
      button.textContent = "Failed";
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = "Download";
    pinterestStatus.hidden = false;
    pinterestStatus.className = "message error";
    pinterestStatus.textContent = error.message;
  }
}

document.addEventListener("click", (event) => {
  const downloadAllButton = event.target.closest("[data-download-all]");
  if (downloadAllButton) {
    downloadAllStaticItems(downloadAllButton);
    return;
  }
  const button = event.target.closest("[data-download-id]");
  if (!button) return;
  downloadStaticItem(button.dataset.downloadId, button);
});
</script>
"""


@router.get("/ui", response_class=HTMLResponse)
def dashboard(db: Session = Depends(get_db)):
    latest = latest_ranking_subquery(db)
    total = db.query(func.count(Item.id)).scalar() or 0
    high = (
        db.query(func.count(Item.id))
        .join(latest, latest.c.item_id == Item.id)
        .join(Ranking, Ranking.id == latest.c.ranking_id)
        .filter(Ranking.drama_score >= 70)
        .scalar()
        or 0
    )
    counts = _source_counts(db)
    latest_run = db.query(Run).order_by(Run.started_at.desc()).first()
    latest_run_text = "No runs yet"
    if latest_run:
        latest_run_text = f"#{latest_run.id} {latest_run.source} {latest_run.status} at {latest_run.started_at}"
    body = f"""
<section class="hero">
  <h1>Drama Clip Scout</h1>
  <p>Local clip lead collection for Hermes. Metadata and links only.</p>
</section>
<section class="stats">
  <div><strong>{total}</strong><span>Total clips</span></div>
  <div><strong>{high}</strong><span>High potential</span></div>
  <div><strong>{counts.get("reddit", 0)}</strong><span>Reddit clips</span></div>
  <div><strong>{counts.get("x", 0)}</strong><span>X posts</span></div>
  <div><strong>{counts.get("kiwifarms", 0)}</strong><span>Kiwi Farms leads</span></div>
</section>
<section class="research-grid">
  <form id="research-form" class="panel research-panel">
    <div class="panel-head">
      <h2>Source</h2>
      <span class="pill">free web search</span>
    </div>
    <div class="research-fields">
      <label>Target<select id="source-choice"><option value="reddit">Reddit</option><option value="x">X / Twitter</option><option value="reddit_x">Reddit + X</option><option value="kiwifarms">Kiwi Farms</option><option value="all">All Sources</option></select></label>
      <label>Subreddit<input id="subreddit" value="{html.escape(get_settings().default_subreddit)}" placeholder="LivestreamFail"></label>
      <label>Reddit URL<input id="reddit-url" placeholder="https://www.reddit.com/r/..."></label>
      <label>X account optional<input id="x-account" placeholder="@DramaAlert"></label>
      <label>Person or topic<input id="person-topic" placeholder="Hasan debate"></label>
      <label class="wide">X URLs<textarea id="x-urls" rows="3" placeholder="https://x.com/user/status/..."></textarea></label>
      <label class="wide">X archive path<input id="x-archive-path" placeholder="/data/x-archive or /data/x-archive/data/tweets.js"></label>
      <p class="field-note wide">The shared person/topic is used for X web discovery and public Kiwi Farms bridge search. Kiwi Farms may return a nonfatal note when the bridge or its guest access is temporarily unavailable.</p>
      <label>Reddit mode<select id="reddit-mode"><option value="hot">hot</option><option value="new">new</option><option value="rising">rising</option><option value="top_day">top day</option><option value="top_week">top week</option></select></label>
      <label>Time window<select id="time-window"><option value="day">day</option><option value="week">week</option><option value="month">month</option><option value="year">this year</option><option value="all">all time</option></select></label>
      <label>Minimum score<input id="min-score" type="number" min="0" max="100" value="0"></label>
      <label>Limit<input id="research-limit" type="number" min="25" max="5000" value="25"></label>
      <label class="checkline"><input id="only-video" type="checkbox" checked> Videos only</label>
      <label class="checkline"><input id="web-search" type="checkbox" checked> Free web search</label>
      <label class="checkline"><input id="deep-search" type="checkbox"> Deep search / more results</label>
      <p class="field-note wide">Turning on deep search widens the default day window to one month. You can choose another time window afterward.</p>
    </div>
  </form>
  <section class="panel prompt-panel">
    <div class="panel-head">
      <h2>Hermes Prompt</h2>
      <a class="button ghost small-button" href="http://127.0.0.1:9119" target="_blank" rel="noreferrer">Open Hermes</a>
    </div>
    <textarea id="hermes-request" rows="8" placeholder="Give me links of clips about a specific person from this subreddit or X account."></textarea>
    <div class="actions command-row">
      <button id="run-research" type="button">Collect + Find</button>
      <button id="collect-only" type="button" class="secondary">Collect Only</button>
      <button id="copy-hermes" type="button" class="secondary">Copy Hermes Prompt</button>
      <button id="copy-urls" type="button" class="secondary">Copy URLs</button>
      <button id="copy-download-command" type="button" class="secondary">Copy yt-dlp Command</button>
      <button id="download-all" type="button" class="secondary">Download all shown</button>
    </div>
    <p class="muted">Latest collection run: {html.escape(latest_run_text)}</p>
    <div id="collection-status" class="message" hidden></div>
    <div id="message" class="message" hidden></div>
    <pre id="hermes-handoff" class="handoff" hidden></pre>
  </section>
</section>
<section class="panel">
  <div class="panel-head">
    <h2>Pinterest Image Research</h2>
    <span class="pill">public pins • original images</span>
  </div>
  <p class="muted">Describe the images you need, or leave this blank to use the person/topic or Hermes request above. The app searches public Pinterest pins and can download up to 20 matching images to data/downloads/pinterest.</p>
  <div class="pinterest-controls" style="grid-template-columns:minmax(220px, 1fr) 110px auto auto;">
    <label>Image request<input id="pinterest-query" maxlength="300" placeholder="moody late-night streamer setup, neon lighting"></label>
    <label>Images<input id="pinterest-limit" type="number" min="1" max="20" value="8"></label>
    <button id="search-pinterest" type="button">Search</button>
    <button id="download-pinterest" type="button" class="secondary" disabled>Download all</button>
  </div>
  <p class="field-note">Public results can be copyrighted. Keep the pin links for provenance and verify usage rights before republishing an image.</p>
  <div id="pinterest-status" class="message" hidden></div>
  <div id="pinterest-results" class="pinterest-gallery"></div>
</section>
<section class="panel">
  <div class="panel-head">
    <h2>Multi-link Downloader</h2>
    <span class="pill">X posts &amp; photos • YouTube • Reddit • Instagram • Twitch • Kick • Rumble</span>
  </div>
  <p class="muted">Paste up to 100 mixed links. Twitch and Kick clips, VODs, and live channels are supported; live channels must currently be streaming. Rumble video and livestream links work too. Instagram videos download normally and photo-only posts save as image files. X photos save as image files, text-only X posts save as PNG screenshots, and Reddit posts with text do too. Every file is saved under data/downloads/link-downloader.</p>
  <textarea id="video-download-urls" rows="8" placeholder="https://x.com/user/status/1234567890&#10;https://www.youtube.com/watch?v=abcdefghijk&#10;https://www.reddit.com/r/videos/comments/abc123/example/&#10;https://www.instagram.com/reel/DbRJmS-pUBT/&#10;https://clips.twitch.tv/ExampleClipSlug&#10;https://kick.com/example/clips/clip_01J8RGZRKHXHXXKJEHGRM932A5&#10;https://rumble.com/v6abcde-example-video.html"></textarea>
  <div class="actions command-row">
    <button id="download-video-links" type="button">Download links</button>
  </div>
  <div id="video-download-status" class="message" hidden></div>
</section>
<section id="research-results" class="clip-list"></section>
<section class="panel">
  <h2>Quick Links</h2>
  <div class="linkgrid">
    <a href="/ui/clips">Browse ranked clips</a>
    <a href="/ui/report">Readable latest report</a>
    <a href="/reports/latest.md">Markdown report</a>
    <a href="/docs">FastAPI docs</a>
  </div>
</section>
<script>
const stopWords = new Set(["about", "after", "also", "and", "are", "clip", "clips", "find", "for", "from", "give", "links", "me", "only", "person", "specific", "streamer", "streamers", "that", "the", "this", "to", "video", "videos", "want", "what", "with"]);
const message = document.getElementById("message");
const collectionStatus = document.getElementById("collection-status");
const results = document.getElementById("research-results");
const handoff = document.getElementById("hermes-handoff");
const videoDownloadStatus = document.getElementById("video-download-status");
const pinterestStatus = document.getElementById("pinterest-status");
const pinterestResults = document.getElementById("pinterest-results");
let currentResults = [];

function esc(value) {{
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[char]));
}}

function splitLines(value) {{
  return value.split(/[\\n,]+/).map((item) => item.trim()).filter(Boolean);
}}

function promptKeywords() {{
  const prompt = document.getElementById("hermes-request").value;
  const person = document.getElementById("person-topic").value;
  const tokens = prompt.toLowerCase().match(/[a-z0-9_@.-]+/g) || [];
  const personTokens = person.toLowerCase().match(/[a-z0-9_@.-]+/g) || [];
  return [...new Set([...personTokens, ...tokens].map((token) => token.replace(/^@/, "")).filter((token) => token.length > 2 && !stopWords.has(token)))].slice(0, 8);
}}

function sourceChoice() {{
  return document.getElementById("source-choice").value;
}}

function rawResearchLimit() {{
  return Math.max(25, Number(document.getElementById("research-limit").value || 25));
}}

function deepSearchEnabled() {{
  return document.getElementById("deep-search").checked;
}}

function activeLimit(maximum = 100) {{
  const limit = rawResearchLimit();
  if (deepSearchEnabled()) return Math.min(maximum, Math.max(25, limit));
  return Math.min(25, limit);
}}

function activeXPageLimit() {{
  const limit = rawResearchLimit();
  if (deepSearchEnabled()) return Math.min(100, Math.max(25, limit));
  return 25;
}}

function showMessage(text, kind = "") {{
  message.hidden = false;
  message.className = "message" + (kind ? " " + kind : "");
  message.textContent = text;
}}

function setBusy(isBusy) {{
  for (const button of document.querySelectorAll("#run-research, #collect-only, #copy-hermes, #copy-urls, #copy-download-command, #download-all")) {{
    button.disabled = isBusy;
  }}
}}

function collectPayloads() {{
  const source = sourceChoice();
  const archiveLimit = activeLimit(5000);
  const webSearchLimit = activeLimit();
  const xPageLimit = activeXPageLimit();
  const redditLimit = activeLimit();
  const redditComments = deepSearchEnabled() ? 5 : 0;
  const prompt = document.getElementById("hermes-request").value.trim();
  const payloads = [];
  if (source === "reddit" || source === "reddit_x" || source === "all") {{
    const subreddit = document.getElementById("subreddit").value.trim();
    const redditUrl = document.getElementById("reddit-url").value.trim();
    payloads.push(["/collect/reddit", {{
      source_mode: "web",
      subreddit: redditUrl ? null : subreddit,
      url: redditUrl || null,
      mode: document.getElementById("reddit-mode").value,
      limit: redditLimit,
      top_comments_limit: redditComments
    }}]);
  }}
  if (source === "x" || source === "reddit_x" || source === "all") {{
    const account = document.getElementById("x-account").value.trim().replace(/^@/, "");
    const person = document.getElementById("person-topic").value.trim();
    const xUrls = splitLines(document.getElementById("x-urls").value);
    const archivePath = document.getElementById("x-archive-path").value.trim();
    const useWebSearch = document.getElementById("web-search").checked && person && !archivePath;
    if (archivePath) {{
      payloads.push(["/collect/x/archive", {{
        path: archivePath,
        account: account || null,
        limit: archiveLimit
      }}]);
    }}
    if (useWebSearch) {{
      payloads.push(["/collect/x/from-web-search", {{
        account: account || null,
        person,
        topic: person,
        search_provider: "web",
        limit: webSearchLimit
      }}]);
    }}
    const shouldUseXWeb = (!archivePath && !useWebSearch) || xUrls.length;
    if (shouldUseXWeb) {{
      payloads.push(["/collect/x", {{
      source_mode: "web",
      accounts: account ? [account] : null,
      urls: xUrls,
      query: prompt || null,
      limit: xPageLimit
    }}]);
    }}
  }}
  if (source === "kiwifarms" || source === "all") {{
    const person = document.getElementById("person-topic").value.trim();
    const query = person || prompt;
    if (!query) throw new Error("Enter a person or topic, or add a Hermes request, before searching Kiwi Farms.");
    payloads.push(["/collect/kiwifarms", {{
      query,
      limit: webSearchLimit,
      max_pages: deepSearchEnabled() ? 25 : 5
    }}]);
  }}
  return payloads;
}}

async function postJson(url, payload) {{
  const response = await fetch(url, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify(payload) }});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error || "Request failed");
  return data;
}}

function searchPayload() {{
  const searchLimit = activeLimit();
  const account = document.getElementById("x-account").value.trim().replace(/^@/, "");
  return {{
    source: sourceChoice(),
    time_window: document.getElementById("time-window").value,
    keywords: promptKeywords(),
    account: account || null,
    min_drama_score: Number(document.getElementById("min-score").value || 0),
    has_video: document.getElementById("only-video").checked ? true : null,
    limit: searchLimit
  }};
}}

function buildHandoffPrompt(payload) {{
  const source = sourceChoice();
  const subreddit = document.getElementById("subreddit").value.trim();
  const redditUrl = document.getElementById("reddit-url").value.trim();
  const account = document.getElementById("x-account").value.trim();
  const person = document.getElementById("person-topic").value.trim();
  const xUrls = splitLines(document.getElementById("x-urls").value);
  const archivePath = document.getElementById("x-archive-path").value.trim();
  const userPrompt = document.getElementById("hermes-request").value.trim();
  return [
    "Use the local Drama Clip Scout API at http://drama-clip-scout:8787/agent/search-clips.",
    `User request: ${{userPrompt || "Find the strongest relevant clip leads and return links."}}`,
    `Source: ${{source}}`,
    subreddit ? `Subreddit: r/${{subreddit}}` : "",
    redditUrl ? `Reddit URL: ${{redditUrl}}` : "",
    account ? `X account: ${{account}}` : "",
    person ? `Person/topic: ${{person}}` : "",
    `Kiwi Farms searched: ${{source === "kiwifarms" || source === "all" ? "yes, through the public guest bridge" : "no"}}`,
    `Search mode: ${{deepSearchEnabled() ? "deep" : "fast"}}`,
    xUrls.length ? `X URLs: ${{xUrls.join(", ")}}` : "",
    archivePath ? `X archive path: ${{archivePath}}` : "",
    `Search payload: ${{JSON.stringify(payload)}}`,
    "Return direct links, titles, source, score, and a short reason. Treat results as leads, not confirmed claims."
  ].filter(Boolean).join("\\n");
}}

function resultActions(item) {{
  const actions = [];
  if (item.source === "kiwifarms") {{
    if (item.is_video && item.url) actions.push(`<a class="button" href="${{esc(item.url)}}" target="_blank" rel="noreferrer">Open media</a>`);
    if (item.permalink) actions.push(`<a class="button ghost" href="${{esc(item.permalink)}}" target="_blank" rel="noreferrer">Open forum post</a>`);
  }} else if (item.url) {{
    actions.push(`<a class="button" href="${{esc(item.url)}}" target="_blank" rel="noreferrer">Open original</a>`);
  }}
  actions.push(`<a class="button ghost" href="/ui/clips/${{item.id}}">View details</a>`);
  if (item.is_video || item.source === "x" || item.source === "reddit") actions.push(`<button class="secondary download-button" type="button" data-download-id="${{esc(item.id)}}">Download</button>`);
  return actions.join("");
}}

function renderResults(items) {{
  currentResults = items;
  if (!items.length) {{
    results.innerHTML = '<section class="panel empty-state">No matching leads found yet.</section>';
    return;
  }}
  results.innerHTML = items.map((item) => `
    <article class="clip-card result-card">
      <div class="thumbwrap">${{item.thumbnail ? `<img src="${{esc(item.thumbnail)}}" alt="thumbnail">` : '<div class="thumb empty">No thumbnail</div>'}}</div>
      <div class="clip-main">
        <div class="row"><span class="badge">${{esc(item.source)}}</span><span class="label">${{esc(item.potential_label)}}</span><strong>${{esc(item.drama_score)}}</strong></div>
        <h2>${{esc(item.title_or_text)}}</h2>
        <p>${{esc(item.reasoning)}}</p>
        <p class="muted">${{esc(item.created_time || "unknown time")}}${{item.author_name ? ` | ${{esc(item.author_name)}}` : ""}}</p>
        <div class="actions small">${{resultActions(item)}}</div>
      </div>
    </article>
  `).join("");
}}

function resultUrls() {{
  return [...new Set(currentResults.map((item) => item.url).filter(Boolean))];
}}

function downloadableResultUrls() {{
  const urls = currentResults.filter((item) => item.is_video).flatMap((item) => {{
    if (item.source !== "kiwifarms") return [item.url].filter(Boolean);
    const mediaUrls = (item.media?.items || []).map((entry) => entry.url).filter(Boolean);
    return mediaUrls.length ? mediaUrls : [item.url].filter(Boolean);
  }});
  return [...new Set(urls)];
}}

function dockerDownloadCommand(urls) {{
  return [
    "cat <<'URLS' | docker exec -i drama-clip-scout sh -lc 'mkdir -p /data/downloads && yt-dlp -P /data/downloads -a -'",
    ...urls,
    "URLS"
  ].join("\\n");
}}

function downloadSummary(data) {{
  if (data.status === "success") {{
    const count = (data.files || []).length;
    return `Downloaded ${{count}} file${{count === 1 ? "" : "s"}} to ${{data.host_dir || data.download_dir}}.`;
  }}
  if (data.status === "partial") {{
    const count = (data.files || []).length;
    return `Downloaded ${{count}} file${{count === 1 ? "" : "s"}}; ${{data.media_failed || 0}} of ${{data.media_count || 0}} media URLs failed. Files are in ${{data.host_dir || data.download_dir}}.`;
  }}
  const detail = (data.stderr_tail || data.stdout_tail || "yt-dlp could not resolve this URL.").trim();
  return `Download failed for item #${{data.item_id}}: ${{detail}}`;
}}

function renderLinkDownloadResult(data) {{
  const total = data.unique_count || 0;
  const counts = Object.entries(data.source_counts || {{}}).map(([source, count]) => `${{source}}: ${{count}}`).join(", ");
  const summary = `Finished: ${{data.succeeded || 0}} of ${{total}} unique links saved inside the app at ${{data.host_dir || "data/downloads/link-downloader"}}.${{counts ? ` Sources: ${{counts}}.` : ""}}${{data.duplicates_skipped ? ` ${{data.duplicates_skipped}} duplicate${{data.duplicates_skipped === 1 ? "" : "s"}} skipped.` : ""}}${{data.invalid_count ? ` ${{data.invalid_count}} invalid link${{data.invalid_count === 1 ? "" : "s"}}.` : ""}}`;
  const rows = (data.downloads || []).map((entry) => {{
    const files = entry.files || [];
    const detail = entry.error || files.length + ` file${{files.length === 1 ? "" : "s"}}`;
    const source = entry.source ? `[${{entry.source}}] ` : "";
    const fileLinks = files.map((file) => {{
      const name = file.name || String(file.host_path || file.path || "downloaded file").split("/").pop();
      const saveLink = file.download_url
        ? `<a class="button ghost small-button" href="${{esc(file.download_url)}}" download>Save file</a>`
        : "";
      return `<li>${{saveLink}} <strong>${{esc(name)}}</strong><br><code>${{esc(file.host_path || file.path || "")}}</code></li>`;
    }}).join("");
    return `<li><span class="status ${{entry.status === "success" ? "ok" : "missing"}}">${{esc(entry.status)}}</span> ${{esc(source)}}${{esc(entry.url || entry.input_url || "unknown link")}} — ${{esc(detail)}}${{fileLinks ? `<ul class="comments">${{fileLinks}}</ul>` : ""}}</li>`;
  }}).join("");
  videoDownloadStatus.hidden = false;
  videoDownloadStatus.className = "message " + (data.status === "success" ? "success" : "error");
  videoDownloadStatus.innerHTML = `<p>${{esc(summary)}}</p><p>Use <strong>Save file</strong> to copy an output into your browser’s Downloads folder.</p>${{rows ? `<ul class="comments">${{rows}}</ul>` : ""}}`;
}}

function effectivePinterestQuery() {{
  return document.getElementById("pinterest-query").value.trim()
    || document.getElementById("person-topic").value.trim()
    || document.getElementById("hermes-request").value.trim();
}}

let pinterestPinsCache = [];
let pinterestQueryCache = "";

function renderPinterestSearchResult(data) {{
  const total = data.pins_found || 0;
  const summary = `Found ${{total}} public pin${{total === 1 ? "" : "s"}}. Press Download all to save the images to data/downloads/pinterest.`;
  pinterestStatus.hidden = false;
  pinterestStatus.className = "message " + (total ? "success" : "error");
  pinterestStatus.innerHTML = `<p>${{esc(summary)}}</p><p>${{esc(data.rights_note || "")}}</p><p><a href="${{esc(data.search_url)}}" target="_blank" rel="noreferrer">Open this search on Pinterest</a></p>`;
  pinterestPinsCache = data.pins || [];
  pinterestQueryCache = data.query || "";
  document.getElementById("download-pinterest").disabled = !pinterestPinsCache.length;
  pinterestResults.innerHTML = (data.pins || []).map((pin) => {{
    const label = pin.title || pin.description || `Pinterest pin ${{pin.pin_id}}`;
    return `<article class="pinterest-card">
      <img src="${{esc(pin.image_url)}}" alt="${{esc(label)}}" referrerpolicy="no-referrer">
      <p>${{esc(label)}}</p>
      <p class="muted">${{pin.pinner ? `@${{esc(pin.pinner)}} • ` : ""}}${{esc(pin.width || "?")}}×${{esc(pin.height || "?")}}</p>
      <div class="actions small">
        <a class="button ghost small-button" href="${{esc(pin.pin_url)}}" target="_blank" rel="noreferrer">Open pin</a>
        <button type="button" class="button ghost small-button" data-pinterest-pin-id="${{esc(pin.pin_id)}}">Download</button>
      </div>
    </article>`;
  }}).join("");
}}

function renderPinterestDownloadResult(data) {{
  const total = data.pins_found || 0;
  const summary = `Downloaded ${{data.succeeded || 0}} image${{data.succeeded === 1 ? "" : "s"}} to ${{data.host_dir || "data/downloads/pinterest"}}.${{data.failed ? ` ${{data.failed}} download${{data.failed === 1 ? "" : "s"}} failed.` : ""}}`;
  pinterestStatus.hidden = false;
  pinterestStatus.className = "message " + (data.failed ? "error" : data.succeeded ? "success" : "error");
  pinterestStatus.innerHTML = `<p>${{esc(summary)}}</p><p>${{esc(data.rights_note || "")}}</p>`;
  pinterestResults.innerHTML = (data.downloads || []).map((entry) => {{
    const label = entry.title || entry.description || `Pinterest pin ${{entry.pin_id}}`;
    if (entry.status !== "success" || !entry.file) {{
      return `<article class="pinterest-card"><span class="status missing">failed</span><p>${{esc(label)}}</p><p class="muted">${{esc(entry.error || "Image download failed")}}</p><a href="${{esc(entry.pin_url)}}" target="_blank" rel="noreferrer">Open pin</a></article>`;
    }}
    return `<article class="pinterest-card">
      <img src="${{esc(entry.file.download_url)}}?inline=true" alt="${{esc(label)}}">
      <p>${{esc(label)}}</p>
      <p class="muted">${{entry.pinner ? `@${{esc(entry.pinner)}} • ` : ""}}${{esc(entry.width || "?")}}×${{esc(entry.height || "?")}}</p>
      <div class="actions small"><a class="button ghost small-button" href="${{esc(entry.pin_url)}}" target="_blank" rel="noreferrer">Open pin</a><a class="button ghost small-button" href="${{esc(entry.file.download_url)}}" download>Save file</a></div>
    </article>`;
  }}).join("");
}}

async function searchPinterest(button) {{
  const query = effectivePinterestQuery();
  const limit = Math.max(1, Math.min(20, Number(document.getElementById("pinterest-limit").value || 8)));
  if (!query) {{
    pinterestStatus.hidden = false;
    pinterestStatus.className = "message error";
    pinterestStatus.textContent = "Describe the images to find, enter a person/topic, or add a Hermes request first.";
    return;
  }}
  if (query.length > 300) {{
    pinterestStatus.hidden = false;
    pinterestStatus.className = "message error";
    pinterestStatus.textContent = "Keep the Pinterest image request to 300 characters or fewer.";
    return;
  }}
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = `Searching for ${{limit}} image${{limit === 1 ? "" : "s"}}...`;
  pinterestResults.innerHTML = "";
  pinterestPinsCache = [];
  document.getElementById("download-pinterest").disabled = true;
  pinterestStatus.hidden = false;
  pinterestStatus.className = "message";
  pinterestStatus.textContent = "Searching public Pinterest pins...";
  try {{
    const data = await postJson("/research/pinterest-search", {{ query, limit }});
    renderPinterestSearchResult(data);
  }} catch (error) {{
    pinterestStatus.className = "message error";
    pinterestStatus.textContent = error.message;
  }} finally {{
    button.disabled = false;
    button.textContent = originalText;
  }}
}}

async function downloadPinterestPins(button) {{
  if (!pinterestPinsCache.length) {{
    pinterestStatus.hidden = false;
    pinterestStatus.className = "message error";
    pinterestStatus.textContent = "Search for Pinterest images first.";
    return;
  }}
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = `Downloading ${{pinterestPinsCache.length}} image${{pinterestPinsCache.length === 1 ? "" : "s"}}...`;
  pinterestStatus.hidden = false;
  pinterestStatus.className = "message";
  pinterestStatus.textContent = "Downloading the original images from Pinterest...";
  try {{
    const data = await postJson("/research/pinterest-download", {{ query: pinterestQueryCache, pins: pinterestPinsCache }});
    renderPinterestDownloadResult(data);
  }} catch (error) {{
    pinterestStatus.className = "message error";
    pinterestStatus.textContent = error.message;
  }} finally {{
    button.disabled = false;
    button.textContent = originalText;
  }}
}}

async function downloadVideoLinks(button) {{
  const urls = splitLines(document.getElementById("video-download-urls").value);
  if (!urls.length) {{
    videoDownloadStatus.hidden = false;
    videoDownloadStatus.className = "message error";
    videoDownloadStatus.textContent = "Paste at least one supported X/Twitter, YouTube, Reddit, Instagram, Twitch, Kick, or Rumble link.";
    return;
  }}
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = `Downloading ${{urls.length}} link${{urls.length === 1 ? "" : "s"}}...`;
  videoDownloadStatus.hidden = false;
  videoDownloadStatus.className = "message";
  videoDownloadStatus.textContent = "Starting downloads. This can take several minutes.";
  try {{
    const data = await postJson("/downloads/links", {{ urls }});
    renderLinkDownloadResult(data);
  }} catch (error) {{
    videoDownloadStatus.className = "message error";
    videoDownloadStatus.textContent = error.message;
  }} finally {{
    button.disabled = false;
    button.textContent = originalText;
  }}
}}

async function downloadItem(itemId, button) {{
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "Downloading...";
  try {{
    const data = await postJson(`/downloads/items/${{itemId}}`, {{}});
    showMessage(downloadSummary(data), data.status === "failed" ? "error" : "success");
  }} catch (error) {{
    showMessage(error.message, "error");
  }} finally {{
    button.disabled = false;
    button.textContent = originalText;
  }}
}}

async function downloadAllResults(button) {{
  const itemIds = [...new Set(currentResults.filter((item) => item.is_video || item.source === "x" || item.source === "reddit").map((item) => String(item.id)).filter(Boolean))];
  if (!itemIds.length) {{
    showMessage("No downloadable video, X post, or Reddit post results are shown.", "error");
    return;
  }}
  const originalText = button.textContent;
  let nextIndex = 0;
  let completed = 0;
  let succeeded = 0;
  let fileCount = 0;
  let failedMedia = 0;
  button.disabled = true;
  const worker = async () => {{
    while (nextIndex < itemIds.length) {{
      const itemId = itemIds[nextIndex++];
      const itemButton = [...results.querySelectorAll("[data-download-id]")].find((candidate) => candidate.dataset.downloadId === itemId);
      if (itemButton) {{
        itemButton.disabled = true;
        itemButton.textContent = "Downloading...";
      }}
      try {{
        const data = await postJson(`/downloads/items/${{itemId}}`, {{}});
        const success = data.status !== "failed";
        succeeded += success ? 1 : 0;
        fileCount += success ? (data.files || []).length : 0;
        failedMedia += data.media_failed || 0;
        if (itemButton) itemButton.textContent = data.status === "success" ? "Downloaded" : data.status === "partial" ? "Partial" : "Retry";
      }} catch (_error) {{
        if (itemButton) itemButton.textContent = "Retry";
      }} finally {{
        if (itemButton) itemButton.disabled = false;
        completed += 1;
        button.textContent = `Downloading ${{completed}}/${{itemIds.length}}`;
        showMessage(`Downloading ${{completed}} of ${{itemIds.length}} shown clips...`);
      }}
    }}
  }};
  try {{
    await Promise.all(Array.from({{ length: Math.min(2, itemIds.length) }}, worker));
    const failed = itemIds.length - succeeded;
    const summary = `Finished: ${{succeeded}} of ${{itemIds.length}} posts downloaded (${{fileCount}} file${{fileCount === 1 ? "" : "s"}}) to data/downloads.${{failedMedia ? ` ${{failedMedia}} individual download attempts failed.` : ""}}${{failed ? ` ${{failed}} items failed; use Retry on those cards.` : ""}}`;
    showMessage(summary, failed ? "error" : "success");
  }} finally {{
    button.disabled = false;
    button.textContent = originalText;
  }}
}}

function collectionSource(url) {{
  if (url.startsWith("/collect/reddit")) return "Reddit";
  if (url.startsWith("/collect/kiwifarms")) return "Kiwi Farms";
  return "X / Twitter";
}}

function renderCollectionStatus(items) {{
  collectionStatus.hidden = false;
  collectionStatus.innerHTML = items.map((item) => {{
    const status = item.status || "unknown";
    const detail = item.note || item.error || `${{item.items_collected || 0}} items collected`;
    return `<div><strong>${{esc(item.collection_source)}}:</strong> <span class="status ${{status === "success" ? "ok" : "missing"}}">${{esc(status)}}</span> ${{esc(detail)}}</div>`;
  }}).join("");
}}

async function collectSources() {{
  const completed = [];
  for (const [url, payload] of collectPayloads()) {{
    const sourceName = collectionSource(url);
    try {{
      completed.push({{ ...(await postJson(url, payload)), collection_source: sourceName }});
    }} catch (error) {{
      completed.push({{ status: "failed", items_collected: 0, error: error.message, collection_source: sourceName }});
    }}
    renderCollectionStatus(completed);
  }}
  const source = sourceChoice();
  const shouldTryRedditXLinks = (source === "x" || source === "reddit_x" || source === "all") && completed.some((item) => item.collection_source === "X / Twitter" && item.source_mode === "web" && (item.items_collected || 0) === 0);
  if (shouldTryRedditXLinks) {{
    showMessage("X timeline was not exposed. Scanning collected Reddit leads for direct X status links...");
    const account = document.getElementById("x-account").value.trim().replace(/^@/, "");
    try {{
      completed.push({{ ...(await postJson("/collect/x/from-reddit", {{
        query: document.getElementById("hermes-request").value.trim() || null,
        accounts: account ? [account] : null,
        time_window: document.getElementById("time-window").value,
        limit: activeLimit()
      }})), collection_source: "X / Twitter (Reddit discovery)" }});
    }} catch (error) {{
      completed.push({{ status: "failed", items_collected: 0, error: error.message, collection_source: "X / Twitter (Reddit discovery)" }});
    }}
    renderCollectionStatus(completed);
  }}
  return completed;
}}

async function runResearch(onlyCollect = false) {{
  setBusy(true);
  currentResults = [];
  results.innerHTML = "";
  handoff.hidden = true;
  collectionStatus.hidden = true;
  collectionStatus.innerHTML = "";
  showMessage(onlyCollect ? "Collecting selected sources..." : "Collecting selected sources and searching leads...");
  try {{
    const collected = await collectSources();
    const notes = collected.map((item) => item.note).filter(Boolean);
    const noteText = notes.length ? " " + notes.join(" ") : "";
    if (onlyCollect) {{
      showMessage("Finished: " + JSON.stringify(collected) + noteText, "success");
      return;
    }}
    const payload = searchPayload();
    const data = await postJson("/agent/search-clips", payload);
    renderResults(data.results || []);
    handoff.textContent = buildHandoffPrompt(payload);
    handoff.hidden = false;
    showMessage(`Finished: collected ${{collected.map((item) => item.items_collected || 0).reduce((a, b) => a + b, 0)}} items and found ${{(data.results || []).length}} leads.${{noteText}}`, "success");
  }} catch (error) {{
    showMessage(error.message, "error");
  }} finally {{
    setBusy(false);
  }}
}}

document.getElementById("run-research").addEventListener("click", () => runResearch(false));
document.getElementById("collect-only").addEventListener("click", () => runResearch(true));
document.getElementById("deep-search").addEventListener("change", (event) => {{
  const timeWindow = document.getElementById("time-window");
  if (event.currentTarget.checked && timeWindow.value === "day") timeWindow.value = "month";
}});
document.getElementById("copy-hermes").addEventListener("click", async () => {{
  const payload = searchPayload();
  const text = buildHandoffPrompt(payload);
  handoff.textContent = text;
  handoff.hidden = false;
  await navigator.clipboard.writeText(text);
  showMessage("Hermes prompt copied.", "success");
}});
document.getElementById("copy-urls").addEventListener("click", async () => {{
  const urls = resultUrls();
  if (!urls.length) {{
    showMessage("No result URLs to copy yet. Run Collect + Find first.", "error");
    return;
  }}
  await navigator.clipboard.writeText(urls.join("\\n"));
  showMessage(`Copied ${{urls.length}} result URL${{urls.length === 1 ? "" : "s"}}.`, "success");
}});
document.getElementById("copy-download-command").addEventListener("click", async () => {{
  const urls = downloadableResultUrls();
  if (!urls.length) {{
    showMessage("No downloadable media URLs are shown. Nonvideo forum leads are links only.", "error");
    return;
  }}
  const command = dockerDownloadCommand(urls);
  handoff.textContent = command;
  handoff.hidden = false;
  await navigator.clipboard.writeText(command);
  showMessage(`Copied Docker yt-dlp command for ${{urls.length}} URL${{urls.length === 1 ? "" : "s"}}.`, "success");
}});
document.getElementById("download-all").addEventListener("click", (event) => downloadAllResults(event.currentTarget));
document.getElementById("search-pinterest").addEventListener("click", (event) => searchPinterest(event.currentTarget));
document.getElementById("download-pinterest").addEventListener("click", (event) => downloadPinterestPins(event.currentTarget));
document.getElementById("download-video-links").addEventListener("click", (event) => downloadVideoLinks(event.currentTarget));
results.addEventListener("click", (event) => {{
  const button = event.target.closest("[data-download-id]");
  if (!button) return;
  downloadItem(button.dataset.downloadId, button);
}});
pinterestResults.addEventListener("click", (event) => {{
  const button = event.target.closest("[data-pinterest-pin-id]");
  if (!button) return;
  downloadSinglePinterestPin(button.dataset.pinterestPinId, button);
}});
</script>
"""
    return page("Dashboard", body)


@router.get("/ui/clips", response_class=HTMLResponse)
def clips_page(
    source: str = Query("all", pattern="^(reddit|x|reddit_x|kiwifarms|all)$"),
    min_drama_score: float = Query(0, ge=0, le=100),
    time_window: str = Query("week", pattern="^(day|week|month|year|all)$"),
    has_video: bool | None = None,
    keyword: str | None = None,
    account: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    rows = query_ranked_items(db, source, min_drama_score, time_window, has_video, keyword, account, limit)
    cards = []
    for item, ranking in rows:
        data = serialize_item(item, ranking)
        thumb = f'<img src="{html.escape(data["thumbnail"])}" alt="thumbnail">' if data.get("thumbnail") else '<div class="thumb empty">No thumbnail</div>'
        metrics = html.escape(json.dumps(data["metrics"], ensure_ascii=True))
        actions = []
        if data["source"] == "kiwifarms":
            if data["is_video"]:
                actions.append(f'<a class="button" href="{html.escape(data["url"])}" target="_blank" rel="noreferrer">Open media</a>')
            if data["permalink"]:
                actions.append(f'<a class="button ghost" href="{html.escape(data["permalink"])}" target="_blank" rel="noreferrer">Open forum post</a>')
        else:
            actions.append(f'<a class="button" href="{html.escape(data["url"])}" target="_blank" rel="noreferrer">Open original</a>')
        actions.append(f'<a class="button ghost" href="/ui/clips/{data["id"]}">View details</a>')
        if data["is_video"] or data["source"] in {"x", "reddit"}:
            actions.append(f'<button class="secondary" type="button" data-download-id="{data["id"]}">Download</button>')
        cards.append(
            f"""
<article class="clip-card">
  <div class="thumbwrap">{thumb}</div>
  <div class="clip-main">
    <div class="row"><span class="badge">{html.escape(data["source"])}</span><span class="label">{html.escape(data["potential_label"])}</span><strong>{data["drama_score"]}</strong></div>
    <h2>{html.escape(data["title_or_text"])}</h2>
    <p>{html.escape(data["reasoning"])}</p>
    <p class="muted">{html.escape(data["created_time"] or "unknown time")} | {html.escape(data["author_name"] or "unknown author")} | {metrics}</p>
    <div class="actions small">{''.join(actions)}</div>
  </div>
</article>"""
        )
    empty = '<section class="panel empty-state">No clips collected yet. Click Collect Reddit to start.</section>' if not cards else ""
    body = f"""
<section class="panel">
  <h1>Clips</h1>
  <form class="filters" method="get">
    <label>Source<select name="source"><option value="all" {"selected" if source == "all" else ""}>all</option><option value="reddit_x" {"selected" if source == "reddit_x" else ""}>reddit + x</option><option value="reddit" {"selected" if source == "reddit" else ""}>reddit</option><option value="x" {"selected" if source == "x" else ""}>x</option><option value="kiwifarms" {"selected" if source == "kiwifarms" else ""}>kiwifarms</option></select></label>
    <label>Minimum score<input type="number" name="min_drama_score" min="0" max="100" value="{min_drama_score}"></label>
    <label>Time window<select name="time_window"><option {"selected" if time_window == "day" else ""}>day</option><option {"selected" if time_window == "week" else ""}>week</option><option {"selected" if time_window == "month" else ""}>month</option><option value="year" {"selected" if time_window == "year" else ""}>this year</option><option {"selected" if time_window == "all" else ""}>all</option></select></label>
    <label>Has video<select name="has_video"><option value="" {"selected" if has_video is None else ""}>any</option><option value="true" {"selected" if has_video is True else ""}>true</option><option value="false" {"selected" if has_video is False else ""}>false</option></select></label>
    <label>Keyword<input name="keyword" value="{html.escape(keyword or "")}" placeholder="search text"></label>
    <label>X account<input name="account" value="{html.escape(account or "")}" placeholder="Awk20000"></label>
    <label>Limit<input type="number" name="limit" min="1" max="200" value="{limit}"></label>
    <button type="submit">Apply</button>
    <button type="button" class="secondary" data-download-all>Download all shown</button>
  </form>
  <div id="message" class="message" hidden></div>
</section>
{empty}
<section class="clip-list">
  {''.join(cards)}
</section>
{_download_controls_script()}
"""
    return page("Clips", body)


@router.get("/ui/clips/{item_id}", response_class=HTMLResponse)
def clip_detail_page(item_id: int, db: Session = Depends(get_db)):
    latest = latest_ranking_subquery(db)
    row = (
        db.query(Item, Ranking)
        .join(latest, latest.c.item_id == Item.id)
        .join(Ranking, Ranking.id == latest.c.ranking_id)
        .filter(Item.id == item_id)
        .one_or_none()
    )
    if not row:
        item = db.get(Item, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Clip not found")
        data = serialize_item(item, None, include_raw=True)
    else:
        data = serialize_item(row[0], row[1], include_raw=True)
    thumbnail = f'<img class="detail-thumb" src="{html.escape(data["thumbnail"])}" alt="thumbnail">' if data.get("thumbnail") else ""
    comments = "".join(
        f"<li><strong>{html.escape(comment.get('author_name') or 'unknown')}</strong>: {html.escape(comment.get('body') or '')}</li>"
        for comment in data.get("comments", [])
    )
    detail_actions = []
    if data["source"] == "kiwifarms":
        if data["is_video"]:
            detail_actions.append(f'<a class="button" href="{html.escape(data["url"])}" target="_blank" rel="noreferrer">Open media</a>')
        if data["permalink"]:
            detail_actions.append(f'<a class="button ghost" href="{html.escape(data["permalink"])}" target="_blank" rel="noreferrer">Open forum post</a>')
    else:
        detail_actions.append(f'<a class="button" href="{html.escape(data["url"])}" target="_blank" rel="noreferrer">Open original post</a>')
    if data["is_video"] or data["source"] == "x":
        detail_actions.append(f'<button class="secondary" type="button" data-download-id="{data["id"]}">Download</button>')
    body = f"""
<section class="panel detail">
  <div class="row"><span class="badge">{html.escape(data["source"])}</span><span class="label">{html.escape(data["potential_label"])}</span><strong>{data["drama_score"]}</strong></div>
  <h1>{html.escape(data["title_or_text"])}</h1>
  {thumbnail}
  <p>{html.escape(data["reasoning"])}</p>
  <p class="muted">{html.escape(data["created_time"] or "unknown time")} | {html.escape(data["author_name"] or "unknown author")}</p>
  <p class="actions">{''.join(detail_actions)}</p>
  <div id="message" class="message" hidden></div>
  <h2>Metrics</h2>
  <pre>{html.escape(json.dumps(data["metrics"], indent=2, ensure_ascii=True))}</pre>
  <h2>Media</h2>
  <pre>{html.escape(json.dumps(data["media"], indent=2, ensure_ascii=True))}</pre>
  <h2>Top Comments</h2>
  <ul class="comments">{comments or "<li>No comments stored.</li>"}</ul>
  <details>
    <summary>Raw metadata</summary>
    <pre>{html.escape(json.dumps(data.get("raw_metadata", {}), indent=2, ensure_ascii=True))}</pre>
  </details>
</section>
{_download_controls_script()}
"""
    return page("Clip Details", body)


@router.get("/ui/runs", response_class=HTMLResponse)
def runs_page(db: Session = Depends(get_db)):
    runs = db.query(Run).order_by(Run.started_at.desc()).limit(100).all()
    rows = "".join(
        f"<tr><td>{run.id}</td><td>{html.escape(run.source)}</td><td>{html.escape(run.mode or '')}</td>"
        f"<td>{html.escape(str(run.started_at or ''))}</td><td>{html.escape(str(run.finished_at or ''))}</td>"
        f"<td>{html.escape(run.status)}</td><td>{run.items_collected}</td><td>{html.escape(run.error or '')}</td></tr>"
        for run in runs
    )
    body = f"""
<section class="panel">
  <h1>Collection Runs</h1>
  <div class="tablewrap"><table>
    <thead><tr><th>Run ID</th><th>Source</th><th>Mode</th><th>Started</th><th>Finished</th><th>Status</th><th>Items</th><th>Errors</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="8">No runs yet.</td></tr>'}</tbody>
  </table></div>
</section>
"""
    return page("Runs", body)


@router.get("/ui/settings", response_class=HTMLResponse)
def settings_page():
    settings = get_settings()
    checks = {
        "REDDIT_CLIENT_ID": settings.reddit_client_id,
        "REDDIT_CLIENT_SECRET": settings.reddit_client_secret,
        "REDDIT_USERNAME": settings.reddit_username,
        "REDDIT_PASSWORD": settings.reddit_password,
        "REDDIT_USER_AGENT": settings.reddit_user_agent,
        "X_BEARER_TOKEN": settings.x_bearer_token,
        "KIWIFARMS_BRIDGE_URL": settings.kiwifarms_bridge_url,
        "KIWIFARMS_BASE_URL (legacy fallback)": settings.kiwifarms_base_url,
        "USER_AGENT": DEFAULT_HEADERS["User-Agent"],
        "OUTBOUND_PROXY_URL": outbound_proxy_url(),
    }
    rows = "".join(
        f"<tr><td>{name}</td><td><span class='status {'ok' if value else 'missing'}'>{'configured' if value else 'missing'}</span></td></tr>"
        for name, value in checks.items()
    )
    body = f"""
<section class="panel">
  <h1>Settings Status</h1>
  <p class="muted">API credentials are optional when collection uses website mode. Secret values are never shown here and are not sent to the browser.</p>
  <table><thead><tr><th>Variable</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>
</section>
"""
    return page("Settings", body)
