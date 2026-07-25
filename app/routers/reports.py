from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.reports import markdown_report

router = APIRouter()


@router.get("/reports/latest.md", response_class=PlainTextResponse)
def latest_report_markdown(
    source: str = Query("all", pattern="^(reddit|x|reddit_x|kiwifarms|all)$"),
    db: Session = Depends(get_db),
):
    return markdown_report(db, source=source)


@router.get("/ui/report", response_class=HTMLResponse)
def latest_report_page(
    source: str = Query("all", pattern="^(reddit|x|reddit_x|kiwifarms|all)$"),
    db: Session = Depends(get_db),
):
    markdown = markdown_report(db, source=source)
    source_options = "".join(
        f"<option value='{value}'{' selected' if source == value else ''}>{label}</option>"
        for value, label in (
            ("all", "All Sources"),
            ("reddit_x", "Reddit + X"),
            ("reddit", "Reddit"),
            ("x", "X / Twitter"),
            ("kiwifarms", "Kiwi Farms"),
        )
    )
    html = (
        "<!doctype html><html><head><title>Latest Report</title>"
        "<link rel='icon' type='image/svg+xml' href='/favicon.svg'>"
        "<link rel='stylesheet' href='/ui.css'></head><body>"
        "<main class='shell'><nav><a href='/ui'>Dashboard</a><a href='/ui/clips'>Clips</a>"
        "<a href='/ui/runs'>Runs</a><a href='/ui/settings'>Settings</a></nav>"
        "<section class='panel'><form method='get' class='actions'><label>Source<select name='source'>"
        + source_options
        + "</select></label><button type='submit'>Apply</button></form></section><section class='panel'><pre class='report'>"
        + markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</pre></section></main></body></html>"
    )
    return HTMLResponse(html)
