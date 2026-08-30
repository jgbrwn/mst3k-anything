"""mst3k-anything backend: FastAPI + SQLite job queue + SSE progress + artifacts.

Design: the pipeline stays the `mst3k` package / `mst3k render` CLI. The API
runs it as an async subprocess per job (isolation + real log tailing -> SSE),
with a reaper done-callback that marks jobs failed if the process dies.
Config comes from the project .env via the mst3k package.
"""
import json
import os
import queue
import shutil
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
        video TEXT, srt TEXT, riffs TEXT,
        provider TEXT, model TEXT, hidden INTEGER DEFAULT 0
      );
    """)
    # Keep existing databases compatible with the current job shape.
    for column in ("provider TEXT", "model TEXT", "hidden INTEGER DEFAULT 0",
                   "judge_provider TEXT", "judge_model TEXT"):
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    # recover jobs interrupted by a restart
    con.execute("UPDATE jobs SET status='failed', error='server restarted' "
                "WHERE status='running'")
    con.commit(); con.close()


async def worker() -> None:
    print("[worker] started", flush=True)
    while True:
        try:
            jid = await asyncio.get_event_loop().run_in_executor(None, _proc.get)
            print(f"[worker] dequeued jid={jid}", flush=True)
        except Exception as e:
            print(f"[worker] dequeue crashed: {e!r}", flush=True)
            import traceback; traceback.print_exc(); await asyncio.sleep(1); continue
        try:
            await run_job(jid)
        except Exception as e:
            print(f"[worker] run_job {jid} crashed: {e!r}", flush=True)
            import traceback; traceback.print_exc()


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
    env = dict(os.environ)
    # make sure ~/.local/bin survives into subprocesses under systemd's PATH
    user_bin = Path.home() / ".local" / "bin"
    if user_bin.exists():
        path = env.get("PATH", "")
        if str(user_bin) not in path:
            env["PATH"] = f"{user_bin}{os.pathsep}{path}"
    env["PYTHONPATH"] = str(ROOT / "src")
    if row["provider"]:
        env["MST3K_PROVIDER"] = row["provider"]
    if row["model"]:
        env["MST3K_MODEL"] = row["model"]
    # optional per-role judge override (separate picker in UI)
    if row["judge_provider"]:
        env["MST3K_JUDGE_PROVIDER"] = row["judge_provider"]
    if row["judge_model"]:
        env["MST3K_JUDGE_MODEL"] = row["judge_model"]
    with open(logpath, "w") as log:
        p = await asyncio.create_subprocess_exec(
            PE, "-m", "mst3k.cli", "render", row["source"],
            stdout=log, stderr=asyncio.subprocess.STDOUT, env=env)
        rc = await p.wait()
    # slug may have been changed to a title-based form after ingest;
    # either dir might hold artifacts. Walk both, keeping the original
    # dir first so log text path resolution stays sane.
    candidates = [job_dir]
    marker = ROOT / "jobs" / f"{row['slug']}.renamed-from"
    if marker.exists():
        try:
            candidates.insert(0, ROOT / "jobs" / marker.read_text().strip())
        except Exception:
            pass
    else:
        # also scan sibling headers — defensive when the marker write raced.
        for p in ROOT.glob("jobs/*"):
            if p.is_dir() and p.name != job_dir.name and p.name.startswith("https-youtu-be"):
                if (p / "source.mp4").exists():
                    candidates.append(p)
    video = next((v for c in candidates for v in c.glob("*_riffed.mp4") if v.exists()), None)
    srt   = next((s for c in candidates for s in c.glob("*_riffs.srt") if s.exists()), None)
    if rc == 0 and video and video.exists():
        final_dir = video.parent
        _set(jid, status="done", video=str(video),
             srt=str(srt) if srt else None,
             riffs=str(final_dir / "riffs.json"))
        # update slug to the title one so list/detail follow the same naming
        if final_dir.name != row["slug"]:
            _set(jid, slug=final_dir.name)
    else:
        err = logpath.read_text()[-4000:] if logpath.exists() else "no log captured"
        _set(jid, status="failed", error=err)


@app.get("/api/providers")
def provider_list():
    from mst3k import providers
    table = providers.load_providers()
    specs = (
        ("hyper", "Hyper", "qwen3.8-flash"),
        ("neuralwatt", "Neuralwatt", "kimi-k3-fast"),
        ("openrouter", "OpenRouter", None),
    )
    result = []
    for pid, label, fallback_model in specs:
        row = table.get(pid, {})
        model = row.get("default_model") if pid != "openrouter" else None
        model = model or fallback_model
        base = row.get("base_url", "")
        base = base.split("://", 1)[-1].split("/", 1)[0]
        result.append({"id": pid,
                       "label": f"{label} ({model})" if model else label,
                       "model": model, "base": base})
    return {"providers": result}


@app.get("/api/providers/openrouter/models")
def openrouter_model_list():
    from mst3k import providers
    try:
        return [{"id": m["id"], "name": m.get("name") or m["id"],
                 "context_length": m.get("context_length", 0)}
                for m in providers.openrouter_models()]
    except Exception as exc:
        raise HTTPException(502, f"could not load OpenRouter models: {exc}")


@app.post("/api/jobs")
async def create_job(req: Request):
    body = await req.json()
    src = (body.get("url") or body.get("source") or "").strip()
    if not src:
        raise HTTPException(400, "url is required")
    provider = body.get("provider") or "hyper"
    allowed = {"hyper", "neuralwatt", "openrouter"}
    if provider not in allowed:
        raise HTTPException(400, "provider must be hyper, neuralwatt, or openrouter")
    model = body.get("model")
    if model is not None:
        if not isinstance(model, str):
            raise HTTPException(400, "model must be a string")
        model = model.strip() or None
    if provider == "openrouter" and model and "/" not in model:
        raise HTTPException(400, "OpenRouter model must include a provider slash (provider/model)")

    # optional judge override
    jprov = (body.get("judge_provider") or "").strip() or None
    jmodel = (body.get("judge_model") or "").strip() or None
    if jprov is not None and jprov not in allowed:
        raise HTTPException(400, "judge_provider must be hyper, neuralwatt, or openrouter")
    if jprov == "openrouter" and jmodel and "/" not in jmodel:
        raise HTTPException(400, "OpenRouter judge_model must be provider/model")

    from mst3k.ingest import slugify
    slug = slugify(src)
    now = time.time()
    con = db()
    cur = con.execute(
        "INSERT INTO jobs(source, slug, status, created, updated, provider, model, "
        "judge_provider, judge_model) "
        "VALUES(?,?, 'queued', ?, ?, ?, ?, ?, ?)",
        (src, slug, now, now, provider, model, jprov, jmodel))
    jid = cur.lastrowid; con.commit(); con.close()
    _proc.put(jid)
    return {"id": jid, "slug": slug, "status": "queued", "provider": provider, "model": model}


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
        "SELECT id, source, slug, status, created, updated, provider, model, "
        "judge_provider, judge_model "
        "FROM jobs WHERE COALESCE(hidden, 0)=0 ORDER BY id DESC LIMIT 50").fetchall()
    con.close()
    return [dict(r) for r in rows]


STAGES = [
    ("ingest", 5),       # download
    ("gaps", 15),        # silence scan
    ("frames", 25),      # frame grabs
    ("transcribe", 35),  # ASR
    ("understand", 45),  # video profile
    ("write", 75),       # + judge pass
    ("synthesize+fit", 90),
    ("mix", 100),
]
STAGE_ORDER = {name: i for i, (name, _) in enumerate(STAGES)}


def _progress(lines: list) -> dict:
    """Compute complete/done-count from [stage] ... done in Ns log lines."""
    done = sum(1 for l in lines if "] done in " in l or l.endswith("done in 0.0s"))
    status_names = set()
    for l in lines:
        if "] done in " in l or l.endswith("done in 0.0s"):
            name = l.lstrip("[").split("]")[0]
            status_names.add(name)
    i = min(len(status_names), len(STAGES) - 1)
    total = len(STAGES)
    next_name = STAGES[i][0] if i < total else None
    pct = int(100 * (i / total))
    return {"done": i, "total": total, "pct": pct, "next_stage": next_name,
            "completed": sorted(status_names)}


@app.get("/api/jobs/{jid}/events")
async def events(jid: int):
    async def gen():
        while True:
            con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
            if not row:
                yield f"data: {json.dumps({'error': 'gone'})}\n\n"; return
            log = ROOT / "jobs" / row["slug"] / "run.log"
            lines = log.read_text().splitlines() if log.exists() else []
            stage = next((l[1:l.index("]")] for l in reversed(lines)
                          if l.startswith("[") and "]" in l), None)
            tail = lines[-80:]
            progress = _progress(lines)
            yield f"data: {json.dumps({'status': row['status'], 'stage': stage, 'log': tail, 'progress': progress})}\n\n"
            if row["status"] in ("done", "failed"):
                yield "event: close\ndata: {}\n\n"; return
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/jobs/{jid}/hide")
def hide_job(jid: int):
    con = db()
    cur = con.execute("UPDATE jobs SET hidden=1, updated=? WHERE id=?", (time.time(), jid))
    con.commit(); con.close()
    if not cur.rowcount:
        raise HTTPException(404, "no such job")
    return {"id": jid, "hidden": True}


def _slug_dirs(slug: str) -> list:
    """All the directories that may hold artifacts for this slug / job.

    The CLI can rename the on-disk dir to a title-based slug after ingest;
    the DB row keeps the URL-derived slug. Rebuild both name forms so
    cancel/delete/~ actually clear the right tree.
    """
    seen = set()
    out = []
    def keep(p):
        if p.is_dir() and str(p) not in seen:
            seen.add(str(p)); out.append(p)
    keep(ROOT / "jobs" / slug)
    # find slug-named dirs (any point in the chain)
    for p in (ROOT / "jobs").iterdir():
        if p.is_dir() and p.name == slug:
            keep(p)
    # follow the rename marker: slug.renamed-to -> final title-slug
    marker = ROOT / "jobs" / f"{slug}.renamed-to"
    if marker.exists():
        try:
            final = marker.read_text().strip()
            if final:
                keep(ROOT / "jobs" / final)
                # protect against chains
                for m2 in (ROOT / "jobs").glob(f"{final}.renamed-to"):
                    keep(ROOT / "jobs" / m2.read_text().strip())
        except Exception:
            pass
    # also resolve: <url-slug>.renamed-from -> slug (helpful when called
    # with a title slug while DB holds the url slug; cheap no-op)
    for p in (ROOT / "jobs").glob(f"*.renamed-from"):
        try:
            if p.read_text().strip() == slug:
                keep(ROOT / "jobs" / p.name.replace(".renamed-from", ""))
        except Exception:
            pass
    return out


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: int):
    """Cancel a queued or running job. Kills the subprocess tree if running."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no such job")
    if row["status"] not in ("queued", "running"):
        con.close()
        return {"id": jid, "status": row["status"], "note": "not in-flight"}
    # signal the worker process first (pid stored in run.log preamble)
    job_dir = _slug_dirs(row["slug"])[0]
    pid_file = job_dir / "pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            import os, signal
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass
    con.execute("UPDATE jobs SET status='canceled', updated=? WHERE id=?",
                (time.time(), jid))
    con.commit(); con.close()
    return {"id": jid, "status": "canceled"}


@app.post("/api/jobs/{jid}/delete")
def delete_job(jid: int):
    """Delete a job via cancel-then-remove. Queued/running jobs get canceled;
    completed/failed/canceled jobs are removed entirely."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "no such job")
    slug = row["slug"]
    # cancel in-flight first
    if row["status"] in ("queued", "running"):
        con.close()
        cancel_job(jid)
        # wait for cancel to settle
        import time as t; t.sleep(0.2)
        con = db(); con.execute("DELETE FROM jobs WHERE id=?", (jid,))
        con.commit(); con.close()
    else:
        con.execute("DELETE FROM jobs WHERE id=?", (jid,))
        con.commit(); con.close()
    # remove all artifact directories that match this slug family
    for d in _slug_dirs(slug):
        shutil.rmtree(d, ignore_errors=True)
    return {"id": jid, "deleted": True}
@app.get("/api/jobs/{jid}/riffs")
def get_riffs(jid: int):
    """The writer output before fit — for in-browser editing."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "no such job")
    riffs = ROOT / "jobs" / row["slug"] / "riffs.json"
    if not riffs.exists():
        raise HTTPException(404, "riffs not written yet")
    return PlainTextResponse(riffs.read_text(), media_type="application/json")


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


@app.post("/api/jobs/{jid}/rerender")
async def rerender(jid: int, req: Request):
    """Accept edited riffs.json, drop downstream caches, re-queue a cheap render."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "no such job")
    if row["status"] == "running":
        raise HTTPException(409, "job is running; wait for it to finish")
    body = await req.body()
    try:
        parsed = json.loads(body)
        if not isinstance(parsed, list):
            raise ValueError("riffs.json must be a JSON array")
    except Exception as e:
        raise HTTPException(400, f"invalid riffs.json: {e}")
    job_dir = ROOT / "jobs" / row["slug"]
    # cheap: upstream artifacts (gaps, frames, profile) survive; downstream drop
    for name in ("riffs.json", "theater.png"):
        p = job_dir / name
        if p.exists():
            p.unlink()
    for d in (job_dir / "tts", job_dir / "segs"):
        if d.exists():
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    # delete any prior outputs so the CLI rebuilds them
    for suffix in ("_riffed.mp4", "_riffs.srt", "final.mp4"):
        p = job_dir / f"{row['slug']}{suffix}"
        if p.exists():
            p.unlink()
    _set(jid, status="queued", video=None, srt=None, riffs=None)
    _proc.put(jid)
    return {"id": jid, "status": "queued"}


import asyncio  # noqa: E402  (after subprocess-free module top for clarity)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    # requeue jobs that never made it or got stranded mid-crash
    con = db()
    con.execute("UPDATE jobs SET status='failed', error='server restarted' "
                "WHERE status='running'")
    for (i,) in con.execute("SELECT id FROM jobs WHERE status='queued'"):
        _proc.put(i)
    con.commit(); con.close()
    asyncio.create_task(worker())


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="ui")
