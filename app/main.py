from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse

from app.database import init_db
from app.routers import agent, clips, collect, downloads, health, reports, ui

app = FastAPI(
    title="Drama Clip Scout",
    description="Local metadata-only collector and ranking API for possible streamer clip leads.",
    version="0.1.0",
)

FAVICON_PATH = Path(__file__).with_name("static") / "favicon.svg"


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", include_in_schema=False)
def root():
    return {"service": "drama-clip-scout", "ui": "/ui", "docs": "/docs", "health": "/health"}


@app.get("/favicon.svg", response_class=FileResponse, include_in_schema=False)
@app.get("/favicon.ico", response_class=FileResponse, include_in_schema=False)
def favicon():
    return FileResponse(FAVICON_PATH, media_type="image/svg+xml")


@app.get("/ui.css", response_class=PlainTextResponse, include_in_schema=False)
def ui_css():
    return PlainTextResponse(
        """
:root { color-scheme: dark; --bg:#101214; --panel:#181c20; --panel2:#20262b; --text:#eef2f3; --muted:#a8b3bd; --line:#303942; --accent:#4cc9a7; --warn:#ffcc66; --bad:#ff7a7a; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height:1.5; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.shell { width:min(1180px, calc(100% - 32px)); margin:0 auto; padding:20px 0 48px; }
.topbar { display:flex; align-items:center; justify-content:space-between; gap:18px; flex-wrap:wrap; margin-bottom:24px; }
.brand { color:var(--text); font-weight:800; font-size:20px; }
nav { display:flex; gap:12px; flex-wrap:wrap; }
nav a { color:var(--muted); }
.hero { padding:28px 0 12px; }
h1 { font-size:32px; margin:0 0 8px; letter-spacing:0; }
h2 { font-size:18px; margin:0 0 12px; letter-spacing:0; }
p { margin:0 0 12px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; margin:0 0 18px; }
.stats { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:12px; margin:10px 0 18px; }
.stats div { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }
.stats strong { display:block; font-size:28px; }
.stats span, .muted { color:var(--muted); }
.actions, .linkgrid { display:flex; gap:10px; flex-wrap:wrap; }
button, .button { display:inline-flex; align-items:center; justify-content:center; min-height:38px; padding:8px 12px; border-radius:8px; border:1px solid var(--accent); background:var(--accent); color:#061311; font-weight:700; cursor:pointer; }
button.secondary { background:transparent; color:var(--accent); }
button:disabled { opacity:.7; cursor:wait; }
.button.ghost { background:transparent; color:var(--accent); }
.small-button { min-height:32px; font-size:13px; padding:5px 9px; }
.small .button { min-height:34px; font-size:14px; }
.message { margin-top:12px; padding:10px 12px; border-radius:8px; background:var(--panel2); border:1px solid var(--line); overflow-wrap:anywhere; }
.message.success { border-color:var(--accent); }
.message.error { border-color:var(--bad); color:var(--bad); }
.filters { display:grid; grid-template-columns:repeat(6, minmax(120px, 1fr)); gap:12px; align-items:end; }
label { display:grid; gap:6px; color:var(--muted); font-size:14px; }
input, select, textarea { width:100%; min-height:38px; border-radius:8px; border:1px solid var(--line); background:var(--panel2); color:var(--text); padding:7px 9px; font:inherit; }
textarea { resize:vertical; line-height:1.45; }
.research-grid { display:grid; grid-template-columns:minmax(340px, .9fr) minmax(360px, 1.1fr); gap:14px; align-items:start; margin-bottom:18px; }
.panel-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
.panel-head h2 { margin:0; }
.pill { display:inline-flex; align-items:center; min-height:26px; border-radius:999px; border:1px solid var(--line); color:var(--muted); padding:3px 9px; font-size:12px; }
.research-fields { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }
.research-fields .wide { grid-column:1 / -1; }
.field-note { margin:-4px 0 0; color:var(--muted); font-size:13px; }
.checkline { display:flex; align-items:center; gap:8px; min-height:38px; align-self:end; }
.checkline input { width:auto; min-height:auto; }
.prompt-panel textarea { min-height:188px; }
.command-row { margin-top:12px; }
.handoff { margin-top:12px; max-height:180px; overflow:auto; }
.pinterest-controls { display:grid; grid-template-columns:minmax(220px, 1fr) 110px auto; gap:12px; align-items:end; }
.pinterest-gallery { display:grid; grid-template-columns:repeat(auto-fill, minmax(190px, 1fr)); gap:12px; margin-top:14px; }
.pinterest-card { background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:10px; min-width:0; }
.pinterest-card img { width:100%; aspect-ratio:4/3; object-fit:cover; border-radius:6px; background:var(--bg); }
.pinterest-card p { margin:8px 0; overflow-wrap:anywhere; }
.clip-list { display:grid; gap:12px; }
.clip-card { display:grid; grid-template-columns:170px minmax(0, 1fr); gap:14px; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
.result-card { border-color:#3c4a54; }
.thumbwrap img, .thumb.empty { width:100%; aspect-ratio:16/10; object-fit:cover; border-radius:6px; background:var(--panel2); color:var(--muted); display:grid; place-items:center; }
.clip-main h2 { margin-top:8px; overflow-wrap:anywhere; }
.row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.badge, .label, .status { display:inline-flex; border-radius:999px; padding:3px 8px; font-size:12px; border:1px solid var(--line); color:var(--muted); }
.label { color:var(--warn); border-color:var(--warn); }
.status.ok { color:var(--accent); border-color:var(--accent); }
.status.missing { color:var(--bad); border-color:var(--bad); }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:12px; }
.report { min-height:70vh; }
.detail-thumb { max-width:420px; width:100%; border-radius:8px; border:1px solid var(--line); margin:8px 0 16px; }
.comments { padding-left:22px; }
.tablewrap { overflow:auto; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; border-bottom:1px solid var(--line); padding:10px; vertical-align:top; }
.empty-state { color:var(--muted); }
@media (max-width: 900px) { .research-grid { grid-template-columns:1fr; } }
@media (max-width: 760px) { .stats { grid-template-columns:repeat(2, minmax(0, 1fr)); } .filters, .research-fields, .pinterest-controls { grid-template-columns:1fr; } .clip-card { grid-template-columns:1fr; } .shell { width:min(100% - 20px, 1180px); } }
""",
        media_type="text/css",
    )


app.include_router(health.router)
app.include_router(collect.router)
app.include_router(clips.router)
app.include_router(downloads.router)
app.include_router(agent.router)
app.include_router(reports.router)
app.include_router(ui.router)
