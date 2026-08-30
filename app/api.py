"""mst3k-anything backend: FastAPI + SQLite job queue + SSE progress + artifacts.

Design: the pipeline stays the `mst3k` package / `mst3k render` CLI. The API
runs it as an async subprocess per job (isolation + real log tailing -> SSE),
with a reaper done-callback that marks jobs failed if the process dies.
Config comes from the project .env via the mst3k package.
"""
import json
import queue
import sqlite3
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mst3k.config import load as load_config  # noqa: E402

CFG = load_config()
PE = str(Path(sys.executable))  # the venv this API runs in
DB = ROOT / "app" / "data" / "jobs.db"

app = FastAPI(title="mst3k-anything")
_proc: queue.Queue = queue.Queue()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = db()
    con.executescript("""
      CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL, slug TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        created REAL, updated REAL, error TEXT,
        video TEXT, srt TEXT, riffs TEXT
      );
    """)
    # recover jobs interrupted by a restart
    con.execute("UPDATE jobs SET status='failed', error='server restarted' "
                "WHERE status='running'")
    con.commit(); con.close()


# --- background worker ---------------------------------------------------
async def worker() -> None:
    while True:
        jid = await asyncio.get_event_loop().run_in_executor(None, _proc.get)
        await run_job(jid)


def _set(jid, **kw):
    con = db()
    con.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time(), jid))
    for k, v in kw.items():
        con.execute(f"UPDATE jobs SET {k}=? WHERE id=?", (v, jid))
    con.commit(); con.close()


async def run_job(jid: int) -> None:
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        return
    _set(jid, status="running")
    job_dir = CFG["jobs_dir"] / row["slug"]
    job_dir.mkdir(parents=True, exist_ok=True)
    logpath = job_dir / "run.log"
    env = {"PYTHONPATH": str(ROOT / "src")}
    with open(logpath, "w") as log:
        p = await asyncio.create_subprocess_exec(
            PE, "-m", "mst3k.cli", "render", row["source"],
            stdout=log, stderr=asyncio.subprocess.STDOUT, env=env)
        rc = await p.wait()
    video = job_dir / f"{row['slug']}_riffed.mp4"
    if rc == 0 and video.exists():
        _set(jid, status="done", video=str(video),
             srt=str(job_dir / f"{row['slug']}_riffs.srt"),
             riffs=str(job_dir / "riffs.json"))
    else:
        err = logpath.read_text()[-4000:]
        _set(jid, status="failed", error=err)


# --- routes ---------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(req: Request):
    body = await req.json()
    src = (body.get("source") or "").strip()
    if not src:
        raise HTTPException(400, "source is required")
    from mst3k.ingest import slugify
    slug = slugify(src)
    con = db()
    cur = con.execute(
        "INSERT INTO jobs(source, slug, status, created, updated) VALUES(?,?, 'queued', ?, ?)",
        (src, slug, time.time(), time.time()))
    jid = cur.lastrowid; con.commit(); con.close()
    _proc.put(jid)
    return {"id": jid, "slug": slug, "status": "queued"}


@app.get("/api/jobs/{jid}")
def get_job(jid: int):
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "no such job")
    d = dict(row)
    if row["status"] != "done":
        d["video"] = d["srt"] = d["riffs"] = None
    return d


@app.get("/api/jobs")
def list_jobs():
    con = db()
    rows = con.execute(
        "SELECT id, source, status, created FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/jobs/{jid}/events")
async def events(jid: int):
    async def gen():
        sent = 0
        while True:
            con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
            if not row:
                yield f"data: {json.dumps({'error': 'gone'})}\n\n"; return
            log = ROOT / "jobs" / row["slug"] / "run.log"
            lines = log.read_text().splitlines() if log.exists() else []
            stage = next((l[1:l.index("]")] for l in reversed(lines)
                          if l.startswith("[") and "]" in l), None)
            yield f"data: {json.dumps({'status': row['status'], 'stage': stage})}\n\n"
            if row["status"] in ("done", "failed"):
                yield "event: close\ndata: {}\n\n"; return
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/jobs/{jid}/{kind}")
def artifact(jid: int, kind: str):
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row or row["status"] != "done":
        raise HTTPException(404, "not ready")
    col = {"video": row["video"], "srt": row["srt"]}.get(kind)
    if not col or not Path(col).exists():
        raise HTTPException(404, "missing artifact")
    mt = "video/mp4" if kind == "video" else "text/plain"
    return FileResponse(col, media_type=mt,
                        filename=Path(col).name)


import asyncio  # noqa: E402  (after subprocess-free module top for clarity)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    asyncio.create_task(worker())


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="ui")
