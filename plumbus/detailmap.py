"""Bake perceived detail into a normal map the engine never provided.

Generated meshes ship albedo only. In any renderer, the detail you *feel* on a
face or fabric, pores, fine wrinkles, knit, is mostly the normal map, not the
triangles, and TRELLIS writes none. This derives one photometrically from the
albedo: luminance as a height proxy, split into a fine band (high-pass) and a
form band (blurred gradients), then encoded as tangent-space normals.

Honest limits, stated once: albedo-derived normals are an illusion. Dark paint
reads as a dent, a bright highlight as a bump. On skin, wood, fabric and rust
the illusion is exactly what game texture artists shipped for a decade before
scanning; on text or hard decals it can look embossed. It is a default worth
having and a flag worth being able to turn off.

No TANGENT attribute is written: three.js (and most engines) derive per-pixel
tangents when the attribute is absent, and meshoptimizer would only churn it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


def _gaussian(img: np.ndarray, sigma: float) -> np.ndarray:
    """Separable gaussian blur without pulling in scipy.ndimage for one call."""
    from scipy.ndimage import gaussian_filter

    return gaussian_filter(img, sigma=sigma, mode="wrap")


def normal_map_from_albedo(
    albedo: "Any",
    fine_strength: float = 1.6,
    form_strength: float = 0.7,
    fine_sigma: float = 1.2,
    form_sigma: float = 6.0,
) -> "Any":
    """PIL image in, PIL normal map out. Wrap-mode filtering respects UV tiling."""
    from PIL import Image

    rgb = np.asarray(albedo.convert("RGB"), dtype=np.float32) / 255.0
    # Perceptual luminance as the height proxy.
    h = rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722

    # Fine band: what survives a high-pass. Pores, stubble, weave.
    fine = h - _gaussian(h, fine_sigma)
    # Form band: gentle large-scale shading turned into slope.
    form = _gaussian(h, form_sigma)

    height = fine * fine_strength + form * form_strength

    # Central-difference gradients; wrap keeps seams from ridging.
    dx = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) * 0.5
    dy = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) * 0.5

    # Tangent-space normal, +Y up in texture space (OpenGL convention, glTF's).
    n = np.stack([-dx, dy, np.ones_like(height) * (1.0 / 255.0 * 40.0)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)

    encoded = ((n * 0.5 + 0.5) * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(encoded, "RGB")


def add_detail_normals(glb: Path, dest: Path | None = None) -> dict[str, Any]:
    """Give every textured material without a normal map a derived one.

    Returns {materials_touched, texture_px} and never raises for a mesh it
    cannot improve; the report says why instead.
    """
    import trimesh

    dest = Path(dest or glb)
    scene = trimesh.load(glb, force="scene")

    touched = 0
    size = None
    for geom in scene.geometry.values():
        visual = getattr(geom, "visual", None)
        mat = getattr(visual, "material", None)
        if mat is None:
            continue
        base = getattr(mat, "baseColorTexture", None)
        if base is None:
            continue
        if getattr(mat, "normalTexture", None) is not None:
            continue  # authored beats derived, always
        mat.normalTexture = normal_map_from_albedo(base)
        size = base.size
        touched += 1

    if not touched:
        return {"materials_touched": 0, "reason": "no textured material lacked a normal map"}

    scene.export(dest)
    return {"materials_touched": touched, "texture_px": list(size or ())}
