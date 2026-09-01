"""Post-generation mesh work: stats, and optional web optimisation."""
from __future__ import annotations

import asyncio
import json
import shutil
import struct
from pathlib import Path
from typing import Any


def _glb_document(glb: Path) -> dict[str, Any]:
    """Read the JSON chunk of a binary glTF without loading its large buffers."""
    with glb.open("rb") as fh:
        magic, version, _length = struct.unpack("<4sII", fh.read(12))
        if magic != b"glTF" or version != 2:
            return {}
        while header := fh.read(8):
            chunk_length, chunk_type = struct.unpack("<II", header)
            payload = fh.read(chunk_length)
            if chunk_type == 0x4E4F534A:  # JSON
                return json.loads(payload.rstrip(b"\x00 \t\r\n"))
    return {}


def production_stats(glb: Path) -> dict[str, Any]:
    """Signals that matter for deformation and material readiness."""
    doc = _glb_document(glb)
    meshes = doc.get("meshes") or []
    primitives = [p for mesh in meshes for p in (mesh.get("primitives") or [])]
    skins = doc.get("skins") or []
    materials = doc.get("materials") or []
    return {
        "skins": len(skins),
        "joints": sum(len(skin.get("joints") or []) for skin in skins),
        "animations": len(doc.get("animations") or []),
        "morph_targets": sum(len(p.get("targets") or []) for p in primitives),
        "has_uv": any("TEXCOORD_0" in (p.get("attributes") or {}) for p in primitives),
        "texture_images": len(doc.get("images") or []),
        "pbr": bool(materials) and any("pbrMetallicRoughness" in m for m in materials),
    }


def mesh_stats(glb: Path) -> dict[str, Any]:
    """Read a GLB without a GPU. Never raises; a bad read just means fewer stats."""
    out: dict[str, Any] = {"bytes": glb.stat().st_size}
    try:
        import trimesh

        scene = trimesh.load(glb, force="scene")
        geoms = list(scene.geometry.values())
        out["vertices"] = int(sum(len(g.vertices) for g in geoms))
        out["faces"] = int(sum(len(g.faces) for g in geoms))
        out["meshes"] = len(geoms)

        bounds = scene.bounds
        if bounds is not None:
            size = (bounds[1] - bounds[0]).tolist()
            out["bbox"] = [round(v, 4) for v in size]

        materials = set()
        has_pbr = False
        for g in geoms:
            mat = getattr(getattr(g, "visual", None), "material", None)
            if mat is None:
                continue
            materials.add(getattr(mat, "name", None) or type(mat).__name__)
            if getattr(mat, "metallicRoughnessTexture", None) is not None:
                has_pbr = True
        out["materials"] = len(materials)
        out["pbr"] = has_pbr
    except Exception as exc:  # noqa: BLE001
        out["stats_error"] = f"{exc.__class__.__name__}: {exc}"

    try:
        # GLB structure is authoritative for skins, animation and PBR declarations.
        out.update(production_stats(glb))
    except Exception as exc:  # noqa: BLE001
        out["production_stats_error"] = f"{exc.__class__.__name__}: {exc}"

    try:
        from .meshrepair import survey

        topology = survey(glb)
        out["topology"] = {
            key: topology.get(key)
            for key in ("welded_vertices", "boundary_edges", "nonmanifold_edges", "bodies", "watertight", "holes")
        }
    except Exception as exc:  # noqa: BLE001
        out["topology_error"] = f"{exc.__class__.__name__}: {exc}"
    return out


async def optimize_for_web(
    glb: Path, target_faces: int | None = None, texture_size: int | None = 1024
) -> tuple[Path | None, str]:
    """Draco + texture compression via gltf-transform. Optional, needs node.

    Returns (optimised_path | None, message). The original is always left intact.
    """
    if not shutil.which("npx"):
        return None, "npx not found, skipping web optimisation"

    dest = glb.with_name(glb.stem + ".web.glb")
    cmd = [
        "npx", "--yes", "@gltf-transform/cli", "optimize",
        str(glb), str(dest),
        "--compress", "draco",
        "--texture-compress", "webp",
    ]
    if texture_size:
        cmd += ["--texture-size", str(texture_size)]
    if target_faces:
        cmd += ["--simplify-error", "0.001"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    msg = (stdout or b"").decode("utf-8", "replace")[-800:]
    if proc.returncode != 0 or not dest.exists():
        return None, f"gltf-transform failed: {msg}"

    before, after = glb.stat().st_size, dest.stat().st_size
    pct = round((1 - after / before) * 100) if before else 0
    return dest, f"web build: {before // 1024}KB -> {after // 1024}KB ({pct}% smaller)"
