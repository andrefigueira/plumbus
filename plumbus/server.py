from __future__ import annotations

import base64
import contextlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import backends, db, gameready, imagegen, worker
from .config import ROOT, settings

WEB = ROOT / "web"
SAFE = re.compile(r"[^a-z0-9\-]+")


def slugify(text: str, fallback: str) -> str:
    s = SAFE.sub("-", text.lower().strip()).strip("-")
    return s[:60] or fallback


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    n = db.reset_orphans()
    if n:
        print(f"[plumbus] requeued {n} job(s) orphaned by the last shutdown")
    worker.start()
    yield
    await worker.stop()


app = FastAPI(title="Plumbus", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """The UI must never be a version behind the server.

    Twice in one day a hard-to-explain bug was really the browser holding
    yesterday's app.js against today's endpoints: a form that reset itself, and
    a feature that half-existed. This is a local tool whose assets change many
    times a session, so /static and / revalidate every load. no-cache still
    permits conditional requests, so unchanged files cost a 304, not a refetch.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ meta

@app.get("/api/backends")
async def api_backends() -> dict[str, Any]:
    infos = backends.describe_all()
    default = settings.plumbus_default_backend
    if not any(b["id"] == default and b["available"] for b in infos):
        default = next((b["id"] for b in infos if b["available"]), default)
    return {"backends": infos, "default": default}


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {"ok": True, "running": worker.current_job_id()}


# ------------------------------------------------------------------ jobs

@app.post("/api/jobs")
async def create_job(
    files: list[UploadFile] = [],
    backend: str = Form(...),
    params: str = Form("{}"),
    prompt: str = Form(""),
    name: str = Form(""),
    image_paths: str = Form("[]"),
) -> dict[str, Any]:
    try:
        parsed: dict = json.loads(params or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"params is not valid JSON: {exc}") from None

    try:
        be = backends.get(backend)
    except backends.BackendError as exc:
        raise HTTPException(400, str(exc)) from None

    ok, reason = be.available()
    if not ok:
        raise HTTPException(409, reason)

    ok, why = be.preflight(parsed)
    if not ok:
        raise HTTPException(422, why)

    try:
        reused_raw: list[str] = json.loads(image_paths or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"image_paths is not valid JSON: {exc}") from None

    reused: list[str] = []
    for name_only in reused_raw:
        # Only ever reach into the upload directory, never wherever the name points.
        cand = (settings.upload_dir / Path(name_only).name).resolve()
        if not cand.is_file() or cand.parent != settings.upload_dir.resolve():
            raise HTTPException(400, f"no such generated image: {name_only}")
        reused.append(str(cand))

    total = len(files) + len(reused)
    if not total and not (be.accepts_text and prompt.strip()):
        raise HTTPException(400, f"{be.name} needs at least one source image.")
    image_limit = be.image_limit(parsed)
    if total > image_limit:
        raise HTTPException(400, f"This {be.name} configuration accepts at most {image_limit} image(s).")

    job_id = db.create_job(backend, [], parsed, prompt.strip() or None)

    saved: list[str] = list(reused)
    for i, f in enumerate(files):
        ext = Path(f.filename or "ref.png").suffix.lower() or ".png"
        if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(400, f"unsupported image type {ext}")
        dest = settings.upload_dir / f"{job_id}_{i}{ext}"
        dest.write_bytes(await f.read())
        saved.append(str(dest))

    parsed.setdefault(
        "_name", slugify(name or prompt or (Path(saved[0]).stem if saved else ""), job_id)
    )
    db.update(job_id, source_images=saved, params=parsed)
    return {"id": job_id}


@app.get("/api/jobs")
async def list_jobs(limit: int = 100) -> dict[str, Any]:
    jobs = db.list_jobs(limit=limit)
    for j in jobs:
        j.pop("log", None)  # keep the list payload small; fetch per job
    return {"jobs": jobs, "running": worker.current_job_id()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = db.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    log = job.get("log") or ""
    job["log"] = log[-20000:]
    return job


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, Any]:
    if not db.get(job_id):
        raise HTTPException(404, "no such job")
    db.delete(job_id)
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = db.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] == "queued":
        db.update(job_id, status="cancelled", stage="cancelled")
        return {"cancelled": job_id}
    if job["status"] == "running" and worker.current_job_id() == job_id:
        worker.cancel_current()
        return {"cancelled": job_id}
    raise HTTPException(409, f"job is {job['status']}, nothing to cancel")


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict[str, Any]:
    job = db.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    new_id = db.create_job(job["backend"], job["source_images"], job["params"], job.get("prompt"))
    return {"id": new_id}


@app.post("/api/jobs/{job_id}/thumb")
async def set_thumb(job_id: str, data_url: str = Form(...)) -> dict[str, Any]:
    """The browser renders the turntable, so it also supplies the thumbnail."""
    if not db.get(job_id):
        raise HTTPException(404, "no such job")
    if "," not in data_url:
        raise HTTPException(400, "expected a data: URL")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    dest = settings.output_dir / f"{job_id}.png"
    dest.write_bytes(raw)
    db.update(job_id, thumb_path=str(dest))
    return {"thumb": f"/outputs/{dest.name}"}


@app.post("/api/jobs/{job_id}/export")
async def export_job(
    job_id: str,
    name: str = Form(""),
    category: str = Form("models"),
    game_ready: bool = Form(True),
    target_height: float | None = Form(None),
    target_tris: int | None = Form(None),
) -> dict[str, Any]:
    """Drop the asset into the parent 3d-assets repo in its house style."""
    job = db.get(job_id)
    if not job or job["status"] != "done" or not job.get("glb_path"):
        raise HTTPException(409, "job has no finished asset to export")

    repo = ROOT.parent
    slug = slugify(name or (job["params"] or {}).get("_name", ""), job_id)
    dest_dir = repo / category / slug
    dest_dir.mkdir(parents=True, exist_ok=True)

    src = Path(job["glb_path"])
    web = src.with_name(src.stem + ".web.glb")
    shutil.copy2(src, dest_dir / f"{slug}.source.glb")
    if web.exists():
        shutil.copy2(web, dest_dir / f"{slug}.web.glb")
    if job.get("thumb_path") and Path(job["thumb_path"]).exists():
        shutil.copy2(job["thumb_path"], dest_dir / "thumb.png")

    stats = job.get("stats") or {}
    profile = (job["params"] or {}).get("_profile") or "object"

    # The headline file is the one an engine can use: real-world scale, base at
    # the origin, normals present, on a triangle budget. The raw generation is
    # kept beside it as .source.glb because it is the better retopology input.
    ready: dict[str, Any] = {}
    if game_ready:
        try:
            ready = await gameready.prepare(
                src, dest_dir / f"{slug}.glb",
                profile=profile,
                target_height=target_height,
                target_tris=target_tris,
            )
        except Exception as exc:  # noqa: BLE001
            shutil.copy2(src, dest_dir / f"{slug}.glb")
            ready = {"notes": [f"game-ready pass failed ({exc.__class__.__name__}: {exc})"]}
    else:
        shutil.copy2(src, dest_dir / f"{slug}.glb")
    after = ready.get("after") or {}
    bbox = after.get("bbox") or stats.get("bbox") or []
    height = f"{bbox[1]} m" if len(bbox) > 1 else "n/a"
    topology = stats.get("topology") or {}
    notes = "\n".join(f"- {n}" for n in ready.get("notes") or []) or "- exported as generated"

    (dest_dir / "README.md").write_text(
        f"""# {slug}

Generated with [Plumbus](../../plumbus) using `{stats.get('engine', job['backend'])}`.

`{slug}.glb` is the one to use. It is placed at real-world scale with its base on
the origin, carries vertex normals, and is on a triangle budget with Draco and
WebP compression. `{slug}.source.glb` is the raw generation, kept because it is
the better input for retopology.

## The asset

| | |
|---|---|
| Height | {height} |
| Origin | base at y=0, centred on x/z |
| Triangles | {after.get('faces', stats.get('faces', 'n/a'))} |
| Vertices | {after.get('vertices', stats.get('vertices', 'n/a'))} |
| Attributes | `{after.get('attributes', 'n/a')}` |
| Size | {round((after.get('bytes') or stats.get('bytes', 0)) / 1024)} KB |
| PBR | {stats.get('pbr', 'n/a')} |

## How it was made

| | |
|---|---|
| Engine | `{stats.get('engine', job['backend'])}` |
| Profile | {profile} |
| Quality | {(job['params'] or {}).get('_quality', 'n/a')} |
| Generation time | {stats.get('seconds', 'n/a')}s |
| Cost | ${job.get('cost_usd') or 0:.2f} |
| Source image | `{Path(job['source_images'][0]).name if job['source_images'] else 'n/a'}` |

## What the finishing pass did

{notes}

## Known limits

Raw generation left {topology.get('holes', 'n/a')} holes and
{topology.get('boundary_edges', 'n/a')} boundary edges across
{topology.get('bodies', 'n/a')} bodies. This is a static mesh with no skeleton,
skin weights or blend shapes. For a deforming character it is a starting point
for retopology, not a finished asset.
""",
        encoding="utf-8",
    )
    return {"exported": str(dest_dir.relative_to(repo)), "gameready": ready}


# ------------------------------------------------------------------ reference images

@app.get("/api/imagegen")
async def api_imagegen() -> dict[str, Any]:
    return {"providers": imagegen.describe_all()}


# Reference drawing is one blocking call, so the only way the browser can show
# real progress is a side channel it polls. Keyed by a token the client makes up.
REF_PROGRESS: dict[str, dict[str, Any]] = {}
REF_PROGRESS_MAX = 32


@app.get("/api/imagegen/progress/{token}")
async def ref_progress(token: str) -> dict[str, Any]:
    return REF_PROGRESS.get(token) or {"done": 0, "total": 0, "stage": "", "finished": False}


@app.post("/api/imagegen")
async def make_reference(
    provider: str = Form("gpt-image-2"),
    subject: str = Form(...),
    params: str = Form("{}"),
    token: str = Form(""),
) -> dict[str, Any]:
    """Draw reference images straight into the upload dir, ready to feed a job."""
    try:
        parsed: dict = json.loads(params or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"params is not valid JSON: {exc}") from None

    try:
        prov = imagegen.get(provider)
    except imagegen.ImageError as exc:
        raise HTTPException(400, str(exc)) from None

    ok, reason = prov.available()
    if not ok:
        raise HTTPException(409, reason)

    views = parsed.pop("views", None) or ["three_quarter"]
    if not isinstance(views, list):
        raise HTTPException(400, "views must be a list")
    face_views = parsed.pop("face_views", None) or []
    if not isinstance(face_views, list):
        raise HTTPException(400, "face_views must be a list")

    stem = f"ref_{uuid.uuid4().hex[:8]}"
    log: list[str] = []

    if token:
        # Cheap bound; these entries are tiny and short-lived.
        while len(REF_PROGRESS) >= REF_PROGRESS_MAX:
            REF_PROGRESS.pop(next(iter(REF_PROGRESS)))
        REF_PROGRESS[token] = {
            "done": 0, "total": len(views) + len(face_views), "stage": "starting",
            "finished": False, "preview": None, "rev": 0,
        }

    async def on_event(msg: str) -> None:
        log.append(msg)
        if not token:
            return
        entry = REF_PROGRESS.get(token)
        if entry is None:
            return
        if msg.startswith("[partial] "):
            # "[partial] <filename> <index>" — the newest partial wins, whichever
            # view produced it. rev is what the browser watches to re-fetch.
            name = msg.split(" ")[1]
            entry["preview"] = f"/uploads/{name}"
            entry["rev"] = entry.get("rev", 0) + 1
        elif msg.startswith("[view] "):
            entry["done"] = min(entry["done"] + 1, entry["total"])
            entry["stage"] = msg[7:]
            entry["preview"] = None
        else:
            entry["stage"] = msg

    req = imagegen.ImageRequest(
        subject=subject,
        out_dir=settings.upload_dir,
        stem=stem,
        views=views,
        face_views=face_views,
        raw_prompt=bool(parsed.get("raw_prompt")),
        params=parsed,
    )
    try:
        result = await prov.generate(req, on_event)
    except imagegen.ImageError as exc:
        if token:
            REF_PROGRESS.pop(token, None)
        raise HTTPException(502, str(exc)) from None
    finally:
        entry = REF_PROGRESS.get(token) if token else None
        if entry is not None:
            entry["finished"] = True

    def label_for(view: str) -> str:
        if view.startswith("face_"):
            base = view[5:]
            return f"Face · {imagegen.VIEW_LABELS.get(base, base)}"
        return imagegen.VIEW_LABELS.get(view, view)

    return {
        "images": [
            {
                "name": img.path.name,
                "url": f"/uploads/{img.path.name}",
                "view": img.view,
                "role": "face" if img.view.startswith("face_") else "body",
                "label": label_for(img.view),
                "prompt": img.prompt,
            }
            for img in result.images
        ],
        "usage": result.usage,
        "log": log,
    }


@app.get("/api/imagegen/preview")
async def preview_prompt(
    subject: str,
    view: str = "three_quarter",
    transparent: bool = True,
    profile: str = "object",
) -> dict[str, str]:
    """Show what the preset actually sends, so it is not a black box."""
    return {"prompt": imagegen.build(subject, view, transparent, profile)}


# ------------------------------------------------------------------ static

app.mount("/outputs", StaticFiles(directory=settings.output_dir), name="outputs")
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    idx = WEB / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "web/index.html missing"}, status_code=500)
    return FileResponse(idx)
