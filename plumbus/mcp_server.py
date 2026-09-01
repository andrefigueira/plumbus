"""Plumbus over MCP, so an agent can make assets the way the UI does.

A thin adapter over the RUNNING HTTP server, not a second implementation: the
queue, database, single-slot worker, repair and finishing pipeline all stay in
one place, and this file only translates tool calls into the same requests the
browser makes. Start the server first (`plumbus serve`); every tool here says so
plainly when it is not up.

Wire-in for Claude Code:

    claude mcp add -s user plumbus /path/to/plumbus/.venv/bin/python -- -m plumbus mcp

or keep the repo's .mcp.json for project scope. The server speaks stdio, so
NOTHING in this module may print to stdout; the protocol owns it.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("PLUMBUS_URL", "http://127.0.0.1:8420").rstrip("/")

# Mirrors the per-profile reference defaults in web/app.js (PROFILES) and the
# per-engine quality presets (QUALITY). Small enough to keep in step by hand;
# if a third copy is ever needed, move them server-side and make everyone ask.
PROFILE_VIEWS: dict[str, list[str]] = {
    "object": ["three_quarter"],
    "character": ["front", "left", "back", "three_quarter"],
    "head": ["front", "left", "right", "three_quarter"],
}
FACE_VIEWS: dict[str, list[str]] = {"character": ["front", "left"]}
REF_SIZE: dict[str, str] = {"object": "1024x1024", "character": "1024x1024", "head": "2048x2048"}
QUALITY: dict[str, dict[str, dict[str, Any]]] = {
    "preview":  {"trellis": {"pipeline_type": "512", "texture_size": "512"},
                 "hunyuan": {"octree_resolution": "128", "steps": 25, "max_faces": 40000}},
    "detailed": {"trellis": {"pipeline_type": "1024", "texture_size": "2048"},
                 "hunyuan": {"octree_resolution": "384", "steps": 50, "max_faces": 200000}},
    "hero":     {"trellis": {"pipeline_type": "1024_cascade", "texture_size": "2048"},
                 "hunyuan": {"octree_resolution": "512", "steps": 65, "max_faces": 350000}},
}
FINISH: dict[str, dict[str, Any]] = {
    "object":    {"repair_holes": True,  "preserve_deformation": False, "drop_fragments_below": 0.002},
    "character": {"repair_holes": False, "preserve_deformation": True,  "drop_fragments_below": 0},
    "head":      {"repair_holes": True,  "preserve_deformation": False, "drop_fragments_below": 0.002},
}

server = MCPServer(
    name="plumbus",
    instructions=(
        "Local image-to-3D. draw_reference makes source images, create_asset queues a mesh "
        "build from them, text_to_asset does both in one call. Jobs run one at a time and a "
        "detailed build takes minutes: poll with job_status or block with wait_for_job. "
        "Reference drawing spends real money via the OpenAI API (rough guide per image: low "
        "$0.006, medium $0.05, high $0.21 at 1024; 4x that at 2048), so keep quality at low "
        "or medium while iterating. The Plumbus server must be running: `plumbus serve`."
    ),
)


def _down(exc: Exception) -> RuntimeError:
    return RuntimeError(
        f"Plumbus server not reachable at {BASE} ({exc.__class__.__name__}). "
        f"Start it with `plumbus serve` (or set PLUMBUS_URL)."
    )


async def _api(method: str, path: str, *, timeout: float = 30, **kw: Any) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, f"{BASE}{path}", **kw)
    except httpx.HTTPError as exc:
        raise _down(exc) from None
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text[:300])
        except Exception:  # noqa: BLE001
            detail = r.text[:300]
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {detail}")
    return r.json()


def _job_brief(j: dict[str, Any], log_lines: int = 0) -> dict[str, Any]:
    stats = j.get("stats") or {}
    out = {
        "id": j["id"],
        "name": (j.get("params") or {}).get("_name") or j["id"],
        "status": j["status"],
        "stage": j.get("stage"),
        "progress": j.get("progress"),
        "profile": (j.get("params") or {}).get("_profile"),
        "error": j.get("error"),
        "triangles": stats.get("faces"),
        "seconds": stats.get("seconds"),
        "glb": f"{BASE}/outputs/{j['id']}.glb" if j["status"] == "done" else None,
        "topology": stats.get("topology"),
    }
    if log_lines and j.get("log"):
        out["log_tail"] = j["log"].splitlines()[-log_lines:]
    return out


@server.tool()
async def engine_status() -> dict[str, Any]:
    """Which 3D engines are usable right now, and whether the worker is busy."""
    data = await _api("GET", "/api/backends")
    jobs = await _api("GET", "/api/jobs")
    return {
        "engines": [
            {"id": b["id"], "name": b["name"], "kind": b["kind"],
             "available": b["available"], "why_not": None if b["available"] else b.get("reason")}
            for b in data["backends"]
        ],
        "default": data.get("default"),
        "worker_busy_with": jobs.get("running"),
    }


@server.tool()
async def draw_reference(
    subject: str,
    profile: str = "object",
    quality: str = "medium",
    views: list[str] | None = None,
    transparent: bool = True,
) -> dict[str, Any]:
    """Draw reference image(s) for a subject with GPT Image 2. Costs real money.

    profile: object | character | head. character also derives head-framed face
    close-ups at 2048 from the same identity; head frames the whole set as a
    MetaHuman-ready face scan. quality: low | medium | high. Returns the image
    names to pass to create_asset. Can take one to several minutes.
    """
    if profile not in PROFILE_VIEWS:
        raise ValueError(f"profile must be one of {sorted(PROFILE_VIEWS)}")
    params = {
        "views": views or PROFILE_VIEWS[profile],
        "face_views": FACE_VIEWS.get(profile, []),
        "quality": quality,
        "size": REF_SIZE[profile],
        "transparent": transparent,
        "partials": "0",
        "asset_profile": profile,
    }
    import json as _json
    r = await _api(
        "POST", "/api/imagegen", timeout=900,
        data={"provider": "gpt-image-2", "subject": subject, "params": _json.dumps(params)},
    )
    return {
        "images": [
            {"name": i["name"], "role": i.get("role", "body"), "label": i.get("label"), "url": f"{BASE}{i['url']}"}
            for i in r["images"]
        ],
        "usage_tokens": r.get("usage"),
    }


@server.tool()
async def create_asset(
    image_names: list[str],
    name: str,
    profile: str = "object",
    quality: str = "detailed",
    backend: str | None = None,
    face_image_names: list[str] | None = None,
) -> dict[str, Any]:
    """Queue mesh generation from reference images already in Plumbus's uploads.

    image_names come from draw_reference (role=body). For a character, pass its
    role=face images as face_image_names and a second <name>-head job is queued
    with head finishing, same as the UI. Returns job ids immediately; the build
    itself takes minutes (see wait_for_job).
    """
    import json as _json
    if profile not in FINISH:
        raise ValueError(f"profile must be one of {sorted(FINISH)}")
    engines = await _api("GET", "/api/backends")
    be = backend or engines.get("default")
    chosen = next((b for b in engines["backends"] if b["id"] == be and b["available"]), None) \
        or next((b for b in engines["backends"] if b["available"]), None)
    if not chosen:
        raise RuntimeError("no engine is usable; run `plumbus doctor`")
    limit = chosen.get("max_images") or 1

    def q_params(prof: str) -> str:
        p = {**QUALITY.get(quality, QUALITY["detailed"]).get(chosen["id"], {}),
             "seed": 42, "web_optimize": True, **FINISH[prof],
             "_profile": prof, "_quality": quality}
        return _json.dumps(p)

    body = await _api("POST", "/api/jobs", timeout=60, data={
        "backend": chosen["id"], "params": q_params(profile), "name": name,
        "image_paths": _json.dumps(image_names[:limit]),
    })
    out: dict[str, Any] = {"job_id": body["id"], "engine": chosen["id"], "queued": [body["id"]]}

    faces = (face_image_names or [])[:limit]
    if profile == "character" and faces:
        head = await _api("POST", "/api/jobs", timeout=60, data={
            "backend": chosen["id"], "params": q_params("head"), "name": f"{name}-head",
            "image_paths": _json.dumps(faces),
        })
        out["head_job_id"] = head["id"]
        out["queued"].append(head["id"])
    return out


@server.tool()
async def text_to_asset(
    subject: str,
    name: str,
    profile: str = "object",
    reference_quality: str = "medium",
    mesh_quality: str = "detailed",
) -> dict[str, Any]:
    """One call from a description to queued mesh job(s): draws the reference
    set, then queues the build (and the head build, for a character). Returns
    job ids plus the reference images used. Drawing costs money and the build
    takes minutes; follow up with wait_for_job or job_status.
    """
    refs = await draw_reference(subject, profile=profile, quality=reference_quality)
    bodies = [i["name"] for i in refs["images"] if i["role"] == "body"]
    faces = [i["name"] for i in refs["images"] if i["role"] == "face"]
    jobs = await create_asset(
        bodies, name, profile=profile, quality=mesh_quality, face_image_names=faces,
    )
    return {**jobs, "references": refs["images"]}


@server.tool()
async def job_status(job_id: str, log_lines: int = 12) -> dict[str, Any]:
    """Status, stage, progress and stats for one job, with a log tail."""
    j = await _api("GET", f"/api/jobs/{job_id}")
    return _job_brief(j, log_lines=log_lines)


@server.tool()
async def wait_for_job(job_id: str, timeout_s: int = 240) -> dict[str, Any]:
    """Block until the job finishes, fails or the timeout passes (cap 570s; a
    detailed build is minutes, so call this repeatedly rather than raising the
    cap). Returns the same shape as job_status."""
    deadline = asyncio.get_event_loop().time() + min(max(timeout_s, 5), 570)
    while True:
        j = await _api("GET", f"/api/jobs/{job_id}")
        if j["status"] not in ("queued", "running") or asyncio.get_event_loop().time() > deadline:
            return _job_brief(j, log_lines=12)
        await asyncio.sleep(3)


@server.tool()
async def list_assets(limit: int = 10) -> list[dict[str, Any]]:
    """Recent jobs, newest first: id, name, status, triangle count."""
    data = await _api("GET", "/api/jobs")
    return [_job_brief(j) for j in data["jobs"][: max(1, min(limit, 50))]]


@server.tool()
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued or running job."""
    return await _api("POST", f"/api/jobs/{job_id}/cancel")


@server.tool()
async def export_asset(
    job_id: str,
    name: str,
    game_ready: bool = True,
    target_height: float | None = None,
    target_tris: int | None = None,
) -> dict[str, Any]:
    """Export a finished job into the 3d-assets repo as a game-ready asset:
    real-world scale, base at the origin, vertex normals, triangle budget,
    Draco+WebP, with the raw mesh kept beside it as .source.glb."""
    data: dict[str, Any] = {"name": name, "category": "models", "game_ready": str(game_ready).lower()}
    if target_height is not None:
        data["target_height"] = str(target_height)
    if target_tris is not None:
        data["target_tris"] = str(target_tris)
    r = await _api("POST", f"/api/jobs/{job_id}/export", timeout=600, data=data)
    ready = r.get("gameready") or {}
    return {"exported_to": r.get("exported"), "notes": ready.get("notes"), "result": ready.get("after")}


def run() -> None:
    # stdout belongs to the protocol. Route every logger to stderr and keep the
    # HTTP client's per-request chatter out of the way.
    import logging
    import sys

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
    server.run("stdio")
