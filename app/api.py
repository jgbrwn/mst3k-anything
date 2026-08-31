"""mst3k-anything backend: FastAPI + SQLite job queue + SSE progress + artifacts.

Design: the pipeline stays the `mst3k` package / `mst3k render` CLI. The API
runs it as an async subprocess per job (isolation + real log tailing -> SSE),
with a reaper done-callback that marks jobs failed if the process dies.
Config comes from the project .env via the mst3k package.
"""
import asyncio
import json
import os
import queue
import shutil
import sqlite3
import sys
import time
import traceback
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
        provider TEXT, model TEXT, hidden INTEGER DEFAULT 0,
        judge_provider TEXT, judge_model TEXT,
        riff_density_bias INTEGER DEFAULT 2,
        work_dir TEXT,
        process_pid INTEGER
      );
    """)
    # Keep existing databases compatible with the current job shape.
    for column in ("provider TEXT", "model TEXT", "hidden INTEGER DEFAULT 0",
                   "judge_provider TEXT", "judge_model TEXT",
                   "riff_density_bias INTEGER DEFAULT 2",
                   "work_dir TEXT", "process_pid INTEGER"):
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    # Backfill provider-default model labels for rows created before the API
    # started resolving them at submission time. NULL means "use provider default".
    try:
        from mst3k import providers as _providers
        table = _providers.load_providers()
        for pid, spec in table.items():
            default = spec.get("default_model")
            if default:
                con.execute("UPDATE jobs SET model=? WHERE provider=? AND (model IS NULL OR model='')",
                            (default, pid))
                con.execute("UPDATE jobs SET judge_model=? WHERE judge_provider=? "
                            "AND (judge_model IS NULL OR judge_model='')", (default, pid))
    except Exception:
        pass
    # recover jobs interrupted by a restart
    con.execute("UPDATE jobs SET status='failed', error='server restarted', process_pid=NULL "
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


def _job_work_dir(row) -> Path:
    """Return this job's private filesystem directory.

    New rows always have work_dir. The slug fallback is only for pre-migration
    rows; it is deliberately not used for new jobs because title slugs collide.
    """
    raw = row["work_dir"] if "work_dir" in row.keys() else None
    return Path(raw) if raw else ROOT / "jobs" / row["slug"]


def _row_dirs(row) -> list[Path]:
    """Directories containing this row's artifacts, without following siblings."""
    out = []
    work = _job_work_dir(row)
    if work not in out:
        out.append(work)
    for col in ("video", "srt", "riffs"):
        value = row[col] if col in row.keys() else None
        if value:
            parent = Path(value).parent
            if parent not in out:
                out.append(parent)
    return out


def _protected_dirs(exclude_id: int) -> set[Path]:
    """Directories still owned/referenced by other DB rows."""
    con = db()
    rows = con.execute("SELECT * FROM jobs WHERE id != ?", (exclude_id,)).fetchall()
    con.close()
    protected = set()
    for row in rows:
        protected.update(_row_dirs(row))
    return protected


def _read_log_tail(paths: list[Path], limit: int = 4000) -> str | None:
    for directory in paths:
        logfile = directory / "run.log"
        if logfile.exists():
            try:
                return logfile.read_text(errors="replace")[-limit:]
            except OSError:
                pass
    return None


def _claim_job(jid: int):
    """Atomically claim a queued job; cancellation wins races with dequeue."""
    con = db()
    cur = con.execute("UPDATE jobs SET status='running', error=NULL, updated=? "
                      "WHERE id=? AND status='queued'", (time.time(), jid))
    row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    con.commit(); con.close()
    return row if cur.rowcount else None


def _current_status(jid: int) -> str | None:
    con = db(); row = con.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    return row["status"] if row else None


async def run_job(jid: int) -> None:
    row = _claim_job(jid)
    if not row:
        return
    work_dir = _job_work_dir(row)
    work_dir.mkdir(parents=True, exist_ok=True)
    logpath = work_dir / "run.log"
    env = dict(os.environ)
    user_bin = Path.home() / ".local" / "bin"
    if user_bin.exists():
        path = env.get("PATH", "")
        if str(user_bin) not in path:
            env["PATH"] = f"{user_bin}{os.pathsep}{path}"
    env["PYTHONPATH"] = str(ROOT / "src")
    env["MST3K_JOB_DIR"] = str(work_dir)
    if row["provider"]:
        env["MST3K_PROVIDER"] = row["provider"]
    if row["model"]:
        env["MST3K_MODEL"] = row["model"]
    if row["judge_provider"]:
        env["MST3K_JUDGE_PROVIDER"] = row["judge_provider"]
    if row["judge_model"]:
        env["MST3K_JUDGE_MODEL"] = row["judge_model"]
    if row["riff_density_bias"] is not None:
        env["MST3K_RIFF_DENSITY_BIAS"] = str(row["riff_density_bias"])

    rc = -1
    try:
        with open(logpath, "w") as log:
            p = await asyncio.create_subprocess_exec(
                PE, "-m", "mst3k.cli", "render", row["source"],
                stdout=log, stderr=asyncio.subprocess.STDOUT, env=env)
            _set(jid, process_pid=p.pid)
            rc = await p.wait()
            _set(jid, process_pid=None)
    except Exception:
        # Keep launch/runtime failures in the same durable log shown by SSE.
        with open(logpath, "a") as log:
            traceback.print_exc(file=log)

    # A canceled process must never be converted back to failed/done after its
    # child exits. A missing row means delete won the race; leave the FS alone.
    status = _current_status(jid)
    if status in (None, "canceled"):
        return

    candidates = [work_dir]
    # Legacy rows created before work_dir migration may have title/url marker
    # dirs. New rows never search arbitrary siblings.
    if not row["work_dir"]:
        candidates.extend(d for d in _slug_dirs(row["slug"]) if d not in candidates)
    video = next((v for c in candidates for v in c.glob("*_riffed.mp4") if v.exists()), None)
    srt = next((s for c in candidates for s in c.glob("*_riffs.srt") if s.exists()), None)

    # Make the human title slug the display slug independently of whether the
    # final LLM/render stage succeeded.
    display_slug = None
    meta_path = work_dir / "meta.json"
    if meta_path.exists():
        try:
            from mst3k.ingest import slugify
            title = json.loads(meta_path.read_text()).get("title") or ""
            if title:
                display_slug = slugify(title)
        except Exception:
            pass

    if rc == 0 and video:
        updates = {"status": "done", "video": str(video),
                   "srt": str(srt) if srt else None,
                   "riffs": str(video.parent / "riffs.json"),
                   "error": None}
        if display_slug:
            updates["slug"] = display_slug
        _set(jid, **updates)
    else:
        updates = {"status": "failed",
                   "error": _read_log_tail(candidates) or "job exited without a log"}
        if display_slug:
            updates["slug"] = display_slug
        _set(jid, **updates)


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

    # resolve display models now so the UI shows exactly what will run.
    # A blank model means "provider default" — store the resolved default so
    # the job list shows real model names, not null.
    from mst3k import providers as _providers
    table = _providers.load_providers()
    if not model:
        model = (table.get(provider) or {}).get("default_model") or None
    if jprov and not jmodel:
        jmodel = (table.get(jprov) or {}).get("default_model")

    # optional density bias: 0=extra-low,1=low,2=default,3=high,4=extra-high
    bias = body.get("riff_density_bias")
    try:
        bias = max(0, min(4, int(bias if bias is not None else 2)))
    except (TypeError, ValueError):
        bias = 2

    from mst3k.ingest import slugify
    slug = slugify(src)
    now = time.time()
    con = db()
    cur = con.execute(
        "INSERT INTO jobs(source, slug, status, created, updated, provider, model, "
        "judge_provider, judge_model, riff_density_bias, work_dir) "
        "VALUES(?,?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
        (src, slug, now, now, provider, model, jprov, jmodel, bias, None))
    jid = cur.lastrowid
    work_dir = ROOT / "jobs" / f"job-{jid}-{slug}"
    con.execute("UPDATE jobs SET work_dir=? WHERE id=?", (str(work_dir), jid))
    con.commit(); con.close()
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
            base = _job_work_dir(row)
            if not base.exists() and not row["work_dir"]:
                # Legacy rows only: follow old rename markers. New rows have a
                # private work_dir and never need a sibling sweep.
                legacy = _slug_dirs(row["slug"])
                if legacy:
                    base = legacy[0]
            logfile = base / "run.log"
            lines = logfile.read_text(errors="replace").splitlines() if logfile.exists() else []
            stage = next((l[1:l.index("]")] for l in reversed(lines)
                          if l.startswith("[") and "]" in l), None)
            tail = lines[-120:]
            progress = _progress(lines)
            yield f"data: {json.dumps({'status': row['status'], 'stage': stage, 'log': tail, 'progress': progress, 'error': row['error']})}\n\n"
            if row["status"] in ("done", "failed", "canceled"):
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


def _slug_dirs(slug: str) -> list[Path]:
    """Resolve legacy URL/title directories without broad slug-prefix globs.

    New rows use a private work_dir and never need this compatibility helper.
    """
    jobs_root = ROOT / "jobs"
    out: list[Path] = []
    seen: set[Path] = set()

    def keep(path: Path):
        path = path.resolve()
        if path.is_dir() and path not in seen:
            seen.add(path); out.append(path)

    keep(jobs_root / slug)
    for suffix in (".renamed-to", ".renamed-from"):
        marker = jobs_root / f"{slug}{suffix}"
        if marker.exists():
            try:
                target = marker.read_text().strip()
                if target and Path(target).name == target:
                    keep(jobs_root / target)
            except OSError:
                pass
    for marker in jobs_root.glob("*.renamed-from"):
        try:
            if marker.read_text().strip() == slug:
                keep(jobs_root / marker.name.removesuffix(".renamed-from"))
        except OSError:
            pass
    return out


def _raw_pid_for_row(row) -> int | None:
    """Get the API-owned child/process-group pid, with legacy pid fallback."""
    try:
        value = row["process_pid"] if "process_pid" in row.keys() else None
        if value:
            return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int((_job_work_dir(row) / "pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_for_row(row) -> int | None:
    pid = _raw_pid_for_row(row)
    if not pid:
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\\0", b" ")
        if b"mst3k.cli" not in cmdline:
            return None
        return pid
    except OSError:
        return None


def _group_alive(row) -> bool:
    pid = _raw_pid_for_row(row)
    if not pid:
        return False
    if not row["process_pid"] and not _pid_for_row(row):
        return False
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        # The CLI may not have called setsid yet; the API-owned child can still
        # be alive in the service's process group.
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    except PermissionError:
        return False


def _signal_row(row, sig) -> bool:
    pid = _raw_pid_for_row(row)
    if not pid:
        return False
    # DB process_pid is created by this API. Legacy pid files must still pass
    # the command-line check before we risk signaling a stale process group.
    if not row["process_pid"] and not _pid_for_row(row):
        return False
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    except PermissionError:
        return False


def _remove_row_markers(row, protected: set[Path]) -> None:
    """Remove only legacy markers owned by this row, never sibling markers."""
    dirs = _row_dirs(row)
    keys = {row["slug"]}
    keys.update(d.name for d in dirs)
    protected_keys = set()
    con = db()
    others = con.execute("SELECT * FROM jobs WHERE id != ?", (row["id"],)).fetchall()
    con.close()
    for other in others:
        odirs = _row_dirs(other)
        if any(d in protected for d in odirs):
            protected_keys.add(other["slug"])
            protected_keys.update(d.name for d in odirs)

    for marker in (ROOT / "jobs").glob("*.renamed-*"):
        stem = marker.name.rsplit(".renamed-", 1)[0]
        try:
            target = marker.read_text().strip()
        except OSError:
            continue
        if stem not in keys and target not in keys:
            continue
        if stem in protected_keys or target in protected_keys:
            continue
        try:
            marker.unlink()
        except OSError:
            pass


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: int):
    """Cancel queued/running work without allowing the worker to resurrect it."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "no such job")
    if row["status"] not in ("queued", "running"):
        return {"id": jid, "status": row["status"], "note": "not in-flight"}
    if row["status"] == "running":
        _signal_row(row, 15)  # SIGTERM; run_job preserves canceled below
    con = db()
    con.execute("UPDATE jobs SET status='canceled', updated=? "
                "WHERE id=? AND status IN ('queued','running')", (time.time(), jid))
    con.commit(); con.close()
    return {"id": jid, "status": "canceled"}


@app.post("/api/jobs/{jid}/delete")
def delete_job(jid: int):
    """Cancel if needed, then remove only this row's private artifacts."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "no such job")

    if row["status"] in ("queued", "running"):
        cancel_job(jid)
        # Give the child a moment to exit before removing its work directory.
        for _ in range(30):
            if not _group_alive(row):
                break
            time.sleep(0.1)
        if _group_alive(row):
            _signal_row(row, 9)  # bounded cleanup; directory is job-private
            for _ in range(20):
                if not _group_alive(row):
                    break
                time.sleep(0.1)

    dirs = _row_dirs(row)
    protected = _protected_dirs(jid)
    con = db(); con.execute("DELETE FROM jobs WHERE id=?", (jid,)); con.commit(); con.close()
    jobs_root = (ROOT / "jobs").resolve()
    for directory in dirs:
        try:
            resolved = directory.resolve()
            if resolved != jobs_root and jobs_root in resolved.parents and resolved not in protected:
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass
    _remove_row_markers(row, protected)
    return {"id": jid, "deleted": True}


@app.get("/api/jobs/{jid}/riffs")
def get_riffs(jid: int):
    """Return the final rendered riff manifest, not the pre-judge draft."""
    con = db(); row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(); con.close()
    if not row:
        raise HTTPException(404, "no such job")
    riffs = Path(row["riffs"]) if row["riffs"] else None
    if not riffs or not riffs.exists():
        own = _job_work_dir(row) / "riffs.json"
        riffs = own if own.exists() else None
    if not riffs and not row["work_dir"]:
        for directory in _slug_dirs(row["slug"]):
            candidate = directory / "riffs.json"
            if candidate.exists():
                riffs = candidate
                break
    if not riffs or not riffs.exists():
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
    """Queue a render from the editor's exact riff manifest."""
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

    work_dir = _job_work_dir(row)
    work_dir.mkdir(parents=True, exist_ok=True)
    requested = work_dir / "requested_riffs.json"
    tmp = requested.with_suffix(".tmp")
    tmp.write_text(json.dumps(parsed, indent=2))
    tmp.replace(requested)
    # Preserve upstream analysis/transcription, discard only render-dependent
    # state. The old final manifest is removed so it cannot be shown as the new
    # result while the rerender is queued.
    for name in ("riffs.json", "drafts.json", "judged_riffs.json", "theater.png"):
        p = work_dir / name
        if p.exists():
            p.unlink()
    for directory in (work_dir / "tts", work_dir / "segs"):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
    for p in list(work_dir.glob("*_riffed.mp4")) + list(work_dir.glob("*_riffs.srt")):
        p.unlink(missing_ok=True)
    for name in ("final.mp4", "riffs.srt"):
        (work_dir / name).unlink(missing_ok=True)
    _set(jid, status="queued", error=None, video=None, srt=None, riffs=None)
    _proc.put(jid)
    return {"id": jid, "status": "queued"}

@app.on_event("startup")
async def startup() -> None:
    init_db()
    # requeue jobs that never made it or got stranded mid-crash
    con = db()
    con.execute("UPDATE jobs SET status='failed', error='server restarted', process_pid=NULL "
                "WHERE status='running'")
    for (i,) in con.execute("SELECT id FROM jobs WHERE status='queued'"):
        _proc.put(i)
    con.commit(); con.close()
    asyncio.create_task(worker())


from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="ui")
