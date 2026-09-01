"""Turn a generated mesh into one asset an engine will accept without fiddling.

Generation gives you a shell normalised to roughly unit size, centred on its own
middle, at whatever triangle count the marching produced. Every one of those is
wrong for a game:

  scale        everything imports the same size regardless of what it is, so a
               hydrant and a character arrive identical and you rescale by hand
  origin       centred in the middle of the mesh, so placing it means finding
               the feet first
  budget       200k triangles is a film asset, not a game one

So this does the three boring things that stand between a generation and a drop-in
asset: put it at real-world size, sit it on its own base at the origin, and bring
it down to a triangle budget. Simplification and compression go through
gltf-transform, which is meshoptimizer underneath and already in the pipeline for
web builds.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import numpy as np

# Sensible real-world heights and triangle budgets per profile. These are
# starting points the caller can override, not rules.
PRESETS: dict[str, dict[str, Any]] = {
    "object":    {"height": None, "tris": 20_000, "texture": 2048},
    "character": {"height": 1.8,  "tris": 60_000, "texture": 2048},
    "head":      {"height": 0.24, "tris": 30_000, "texture": 2048},
}


def preset_for(profile: str) -> dict[str, Any]:
    return dict(PRESETS.get(profile) or PRESETS["object"])


def _split_factor(stats: dict[str, Any]) -> float:
    """How far the vertex count is above what a closed manifold would need.

    A closed triangle mesh has roughly half as many vertices as triangles. Every
    UV seam splits a vertex, so a generated mesh runs far above that, and this
    ratio is the single best predictor of how far it will decimate.
    """
    faces = stats.get("faces") or 0
    verts = stats.get("vertices") or 0
    return (verts / (faces / 2)) if faces else 0.0


def normalise(src: Path, dest: Path, target_height: float | None = None) -> dict[str, Any]:
    """Scale to a real height, drop the base to y=0 and centre it on x/z.

    The transform is applied to the scene graph rather than baked into vertex
    positions where it can be helped, so UVs, materials and any skinning are
    untouched.
    """
    import trimesh

    scene = trimesh.load(src, force="scene")
    bounds = scene.bounds
    if bounds is None:
        shutil.copy2(src, dest)
        return {"scaled": False, "reason": "no bounds"}

    lo, hi = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    size = hi - lo
    before_height = float(size[1])

    scale = 1.0
    if target_height and before_height > 1e-9:
        scale = float(target_height) / before_height

    matrix = np.eye(4)
    matrix[0, 0] = matrix[1, 1] = matrix[2, 2] = scale
    # After scaling, sit the base on the ground and centre the footprint.
    matrix[0, 3] = -((lo[0] + hi[0]) / 2.0) * scale
    matrix[1, 3] = -lo[1] * scale
    matrix[2, 3] = -((lo[2] + hi[2]) / 2.0) * scale

    scene.apply_transform(matrix)

    # TRELLIS exports POSITION and TEXCOORD_0 only. With no NORMAL attribute the
    # glTF spec says render it flat, so every engine either faces the model or
    # guesses its own smoothing. Compute them once here instead.
    for geom in scene.geometry.values():
        try:
            if getattr(geom, "vertex_normals", None) is None or not len(geom.vertex_normals):
                geom.rezero  # noqa: B018  (touch to force lazy caches)
        except Exception:  # noqa: BLE001
            pass
        try:
            _ = geom.vertex_normals   # trimesh computes and caches smooth normals
        except Exception:  # noqa: BLE001
            pass

    scene.export(dest)

    after = np.asarray(scene.bounds, float)
    return {
        "scaled": True,
        "scale": round(scale, 6),
        "height_before": round(before_height, 4),
        "height_after": round(float(after[1][1] - after[0][1]), 4),
        "origin": "base at y=0, centred on x/z",
    }


async def compress(
    src: Path,
    dest: Path,
    target_tris: int | None = None,
    current_tris: int | None = None,
    texture_size: int | None = 2048,
    simplify_error: float = 0.02,
) -> tuple[bool, str]:
    """Simplify toward a triangle budget and compress, via gltf-transform.

    `--simplify-ratio` is a fraction of *vertices* to keep, not a triangle
    count, so the budget has to be converted. The error tolerance also has to be
    loosened well past its 0.0001 default: meshoptimizer stops at whichever of
    ratio and error it reaches first, and at the default it stops almost
    immediately and quietly ignores the ratio.
    """
    if not shutil.which("npx"):
        return False, "npx not found, skipping simplification and compression"

    cmd = [
        "npx", "--yes", "@gltf-transform/cli", "optimize",
        str(src), str(dest),
        "--compress", "draco",
        "--texture-compress", "webp",
    ]
    if texture_size:
        cmd += ["--texture-size", str(texture_size)]

    if target_tris and current_tris and current_tris > target_tris:
        ratio = max(0.01, min(1.0, target_tris / current_tris))
        cmd += [
            "--simplify", "true",
            "--simplify-ratio", f"{ratio:.4f}",
            "--simplify-error", str(simplify_error),
        ]
    else:
        cmd += ["--simplify", "false"]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    msg = (out or b"").decode("utf-8", "replace")[-600:]
    if proc.returncode != 0 or not dest.exists():
        return False, f"gltf-transform failed: {msg}"
    return True, msg


async def inspect(glb: Path) -> dict[str, Any]:
    """Triangle count and bounds of a finished GLB, via gltf-transform.

    trimesh cannot decode KHR_draco_mesh_compression and silently reports zeros,
    so anything measured after compression has to come from a tool that can.
    """
    out: dict[str, Any] = {"bytes": glb.stat().st_size}
    if not shutil.which("npx"):
        return out
    proc = await asyncio.create_subprocess_exec(
        "npx", "--yes", "@gltf-transform/cli", "inspect", "--format", "csv", str(glb),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    text = (stdout or b"").decode("utf-8", "replace")

    section = None
    for line in text.splitlines():
        line = line.strip()
        if line in ("SCENES", "MESHES"):
            section = line
            continue
        if not line or line.startswith(("#,", "key,")):
            continue
        cells = _csv_cells(line)
        if section == "SCENES" and len(cells) >= 5 and cells[0].isdigit():
            try:
                lo = [float(v) for v in cells[3].split(",")]
                hi = [float(v) for v in cells[4].split(",")]
                out["bbox"] = [round(h - l, 4) for l, h in zip(lo, hi)]
                out["bbox_min"] = [round(v, 4) for v in lo]
            except Exception:  # noqa: BLE001
                pass
            section = None
        elif section == "MESHES" and len(cells) >= 6 and cells[0].isdigit():
            try:
                out["faces"] = int(cells[4])
                out["vertices"] = int(cells[5])
                out["attributes"] = cells[7] if len(cells) > 7 else ""
            except Exception:  # noqa: BLE001
                pass
            section = None
    return out


def _csv_cells(line: str) -> list[str]:
    """Split a CSV row, keeping quoted commas together."""
    cells, cur, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


async def prepare(
    src: Path,
    dest: Path,
    profile: str = "object",
    target_height: float | None = None,
    target_tris: int | None = None,
    texture_size: int | None = None,
) -> dict[str, Any]:
    """Full pass: real-world scale, origin at the base, triangle budget, compressed.

    Never destroys the source. If any stage fails the best result so far is what
    lands at `dest`, and the report says which stage gave up.
    """
    from .postprocess import mesh_stats

    spec = preset_for(profile)
    height = target_height if target_height is not None else spec["height"]
    tris = target_tris if target_tris is not None else spec["tris"]
    tex = texture_size if texture_size is not None else spec["texture"]

    before = mesh_stats(src)
    staging = dest.with_name(dest.stem + ".staging.glb")
    notes: list[str] = []

    try:
        placed = normalise(src, staging, height)
        notes.append(
            f"placed: {placed.get('height_before')} -> {placed.get('height_after')} units tall, "
            f"base at y=0"
            if placed.get("scaled") else f"placement skipped ({placed.get('reason')})"
        )
    except Exception as exc:  # noqa: BLE001
        shutil.copy2(src, staging)
        notes.append(f"placement skipped ({exc.__class__.__name__}: {exc})")

    current = before.get("faces")
    ok, msg, after = False, "", {}
    # Loosen the tolerance only as far as the budget actually needs. Stopping at
    # the first tolerance that lands within 15% keeps the mesh as tight as it can
    # be rather than always paying for the loosest setting.
    for tolerance in (0.02, 0.06, 0.15):
        ok, msg = await compress(
            staging, dest,
            target_tris=tris,
            current_tris=current,
            texture_size=tex,
            simplify_error=tolerance,
        )
        if not ok:
            break
        after = await inspect(dest)
        got = after.get("faces") or 0
        if not tris or not current or current <= tris or got <= tris * 1.15:
            notes.append(
                f"budget: {got:,} tris against a {tris:,} target "
                f"(error tolerance {tolerance}), {tex}px textures, Draco + WebP"
            )
            break
    else:
        # Not a tolerance problem. Probed directly, ratio 0.05 at error 1.0 gives
        # the same result as ratio 0.29 at error 0.15, so something structural is
        # the floor: meshoptimizer will not collapse a vertex that is split across
        # an attribute seam, and a generated mesh is split almost everywhere.
        split = _split_factor(before)
        why = (
            f"the mesh carries {before.get('vertices', 0):,} vertices for "
            f"{before.get('faces', 0):,} triangles, about {split:.1f}x what a clean "
            f"manifold needs, all split along UV seams, and split vertices cannot "
            f"be collapsed. Retopology is the only thing that lifts this floor."
        ) if split > 1.4 else "the simplifier reached its structural floor for this mesh."
        notes.append(
            f"budget: floored at {after.get('faces', 0):,} tris against a {tris:,} "
            f"target. Not a tolerance limit, {why}"
        )

    if not ok:
        shutil.move(str(staging), str(dest))
        notes.append(msg)
        after = await inspect(dest)
    else:
        staging.unlink(missing_ok=True)
    return {
        "before": before,
        "after": after,
        "target_height": height,
        "target_tris": tris,
        "split_factor": round(_split_factor(before), 2),
        "notes": notes,
    }
