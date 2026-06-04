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
</section>
<section class="panel">
  <h2>Collect</h2>
  <div class="actions">
    <button data-collect="/collect/reddit">Collect Reddit</button>
    <button data-collect="/collect/x">Collect X</button>
    <button data-collect="/collect/all">Collect All</button>
  </div>
  <p class="muted">Latest collection run: {html.escape(latest_run_text)}</p>
  <div id="message" class="message" hidden></div>
</section>
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
for (const button of document.querySelectorAll("[data-collect]")) {{
  button.addEventListener("click", async () => {{
    const message = document.getElementById("message");
    const old = button.textContent;
    button.disabled = true;
    button.textContent = "Collecting...";
    message.hidden = false;
    message.className = "message";
    message.textContent = "Collection is running. This can take a little while.";
    try {{
      const response = await fetch(button.dataset.collect, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: "{{}}" }});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Collection failed");
      message.className = "message success";
      message.textContent = "Finished: " + JSON.stringify(data);
    }} catch (error) {{
      message.className = "message error";
      message.textContent = error.message;
    }} finally {{
      button.disabled = false;
      button.textContent = old;
    }}
  }});
}}
</script>
"""
    return page("Dashboard", body)


@router.get("/ui/clips", response_class=HTMLResponse)
def clips_page(
    source: str = Query("all", pattern="^(reddit|x|all)$"),
    min_drama_score: float = Query(0, ge=0, le=100),
    time_window: str = Query("week", pattern="^(day|week|all)$"),
    has_video: bool | None = None,
    keyword: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    rows = query_ranked_items(db, source, min_drama_score, time_window, has_video, keyword, limit)
    cards = []
    for item, ranking in rows:
        data = serialize_item(item, ranking)
        thumb = f'<img src="{html.escape(data["thumbnail"])}" alt="thumbnail">' if data.get("thumbnail") else '<div class="thumb empty">No thumbnail</div>'
        metrics = html.escape(json.dumps(data["metrics"], ensure_ascii=True))
        cards.append(
            f"""
<article class="clip-card">
  <div class="thumbwrap">{thumb}</div>
  <div class="clip-main">
    <div class="row"><span class="badge">{html.escape(data["source"])}</span><span class="label">{html.escape(data["potential_label"])}</span><strong>{data["drama_score"]}</strong></div>
    <h2>{html.escape(data["title_or_text"])}</h2>
    <p>{html.escape(data["reasoning"])}</p>
    <p class="muted">{html.escape(data["created_time"] or "unknown time")} | {metrics}</p>
    <div class="actions small">
      <a class="button" href="{html.escape(data["url"])}" target="_blank" rel="noreferrer">Open original</a>
      <a class="button ghost" href="/ui/clips/{data["id"]}">View details</a>
    </div>
  </div>
</article>"""
        )
    empty = '<section class="panel empty-state">No clips collected yet. Click Collect Reddit to start.</section>' if not cards else ""
    body = f"""
<section class="panel">
  <h1>Clips</h1>
  <form class="filters" method="get">
    <label>Source<select name="source"><option {"selected" if source == "all" else ""}>all</option><option {"selected" if source == "reddit" else ""}>reddit</option><option {"selected" if source == "x" else ""}>x</option></select></label>
    <label>Minimum score<input type="number" name="min_drama_score" min="0" max="100" value="{min_drama_score}"></label>
    <label>Time window<select name="time_window"><option {"selected" if time_window == "day" else ""}>day</option><option {"selected" if time_window == "week" else ""}>week</option><option {"selected" if time_window == "all" else ""}>all</option></select></label>
    <label>Has video<select name="has_video"><option value="" {"selected" if has_video is None else ""}>any</option><option value="true" {"selected" if has_video is True else ""}>true</option><option value="false" {"selected" if has_video is False else ""}>false</option></select></label>
    <label>Keyword<input name="keyword" value="{html.escape(keyword or "")}" placeholder="search text"></label>
    <label>Limit<input type="number" name="limit" min="1" max="200" value="{limit}"></label>
    <button type="submit">Apply</button>
  </form>
</section>
{empty}
<section class="clip-list">
  {''.join(cards)}
</section>
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
    body = f"""
<section class="panel detail">
  <div class="row"><span class="badge">{html.escape(data["source"])}</span><span class="label">{html.escape(data["potential_label"])}</span><strong>{data["drama_score"]}</strong></div>
  <h1>{html.escape(data["title_or_text"])}</h1>
  {thumbnail}
  <p>{html.escape(data["reasoning"])}</p>
  <p><a class="button" href="{html.escape(data["url"])}" target="_blank" rel="noreferrer">Open original post</a></p>
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
