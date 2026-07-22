from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.reports import markdown_report

router = APIRouter()


@router.get("/reports/latest.md", response_class=PlainTextResponse)
def latest_report_markdown(db: Session = Depends(get_db)):
    return markdown_report(db)


@router.get("/ui/report", response_class=HTMLResponse)
def latest_report_page(db: Session = Depends(get_db)):
    markdown = markdown_report(db)
    html = (
        "<!doctype html><html><head><title>Latest Report</title>"
        "<link rel='icon' type='image/svg+xml' href='/favicon.svg'>"
        "<link rel='stylesheet' href='/ui.css'></head><body>"
        "<main class='shell'><nav><a href='/ui'>Dashboard</a><a href='/ui/clips'>Clips</a>"
        "<a href='/ui/runs'>Runs</a><a href='/ui/settings'>Settings</a></nav>"
        "<section class='panel'><pre class='report'>"
        + markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</pre></section></main></body></html>"
    )
    return HTMLResponse(html)
