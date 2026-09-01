"""Single-slot job worker.

One at a time on purpose: these models will happily eat all 64GB, and two
concurrent MPS runs is how you get a swap storm instead of two meshes.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from . import backends, db
from .config import settings
from .meshrepair import repair as repair_mesh
from .postprocess import mesh_stats, optimize_for_web, production_stats

_task: asyncio.Task | None = None
_current: str | None = None
_cancel = asyncio.Event()


def current_job_id() -> str | None:
    return _current


async def _run_one(job: dict) -> None:
    global _current
    _current = job["id"]
    job_id = job["id"]
    last_push = 0.0

    async def on_event(stage: str, progress: float | None, line: str | None) -> None:
        nonlocal last_push
        if line:
            db.append_log(job_id, line)
        now = time.time()
        # Throttle status writes; the log already carries the detail.
        if now - last_push > 0.4 or progress is not None:
            fields: dict = {"stage": stage}
            if progress is not None:
                fields["progress"] = max(0.0, min(1.0, progress))
            db.update(job_id, **fields)
            last_push = now

    db.update(job_id, status="running", started_at=time.time(), stage="starting", progress=0.0)

    try:
        backend = backends.get(job["backend"])
        ok, reason = backend.available()
        if not ok:
            raise backends.BackendError(reason)
        req = backends.GenerateRequest(
            job_id=job_id,
            images=[Path(p) for p in job["source_images"]],
            out_dir=settings.output_dir,
            prompt=job.get("prompt"),
            params=job.get("params") or {},
        )
        result = await backend.generate(req, on_event)

        params = job.get("params") or {}
        if params.get("preserve_deformation"):
            db.append_log(
                job_id,
                "[topology] preserving the generated surface; static hole filling skipped for downstream retopology",
            )
        elif params.get("repair_holes", True):
            # Trimesh repair flattens the scene. Never run it over an engine that
            # has already supplied skin weights or a skeleton.
            try:
                has_skin = bool(production_stats(result.glb_path).get("skins"))
            except Exception:  # noqa: BLE001
                has_skin = False
            if has_skin:
                db.append_log(job_id, "[repair] skipped to preserve the supplied skeleton and skin weights")
            else:
                await on_event("repairing", 0.93, None)
                try:
                    r = repair_mesh(
                        result.glb_path,
                        min_body_frac=float(params.get("drop_fragments_below") or 0.0),
                    )
                    b, a = r["before"], r["after"]
                    db.append_log(
                        job_id,
                        f"[repair] holes {b['holes']} -> {a['holes']}, "
                        f"boundary edges {b['boundary_edges']} -> {a['boundary_edges']}, "
                        f"bodies {b['bodies']} -> {a['bodies']}, "
                        f"+{r['faces_added']} faces, -{r['faces_dropped']} stray",
                    )
                    if r["skipped_large_openings"]:
                        db.append_log(
                            job_id,
                            f"[repair] left {r['skipped_large_openings']} large opening(s) alone; "
                            f"at that size they are meant to be open",
                        )
                except Exception as exc:  # noqa: BLE001
                    # A repair failure must never cost you the mesh you just waited for.
                    db.append_log(job_id, f"[repair] skipped ({exc.__class__.__name__}: {exc})")

        await on_event("measuring", 0.95, None)
        stats = {**result.stats, **mesh_stats(result.glb_path)}

        if (job.get("params") or {}).get("web_optimize"):
            await on_event("optimising", 0.97, None)
            web, msg = await optimize_for_web(result.glb_path)
            db.append_log(job_id, msg)
            if web:
                stats["web_glb"] = web.name
                stats["web_bytes"] = web.stat().st_size

        db.update(
            job_id,
            status="done",
            progress=1.0,
            stage="done",
            glb_path=str(result.glb_path),
            cost_usd=result.cost_usd,
            stats=stats,
            finished_at=time.time(),
        )
        db.append_log(job_id, f"finished in {stats.get('seconds', '?')}s")

    except asyncio.CancelledError:
        db.update(job_id, status="cancelled", stage="cancelled", finished_at=time.time())
        db.append_log(job_id, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        db.update(
            job_id,
            status="failed",
            stage="failed",
            error=f"{exc.__class__.__name__}: {exc}",
            finished_at=time.time(),
        )
        db.append_log(job_id, f"FAILED: {exc}")
    finally:
        _current = None


async def _loop() -> None:
    while not _cancel.is_set():
        job = db.next_queued()
        if job is None:
            await asyncio.sleep(0.6)
            continue
        await _run_one(job)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _cancel.clear()
        _task = asyncio.create_task(_loop(), name="plumbus-worker")


async def stop() -> None:
    global _task
    _cancel.set()
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None


def cancel_current() -> bool:
    """Kill the in-flight job. The loop picks up the next one."""
    if _task and _current:
        _task.cancel()
        start()
        return True
    return False
