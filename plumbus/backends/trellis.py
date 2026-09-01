"""TRELLIS.2 on Apple Silicon via the trellis-mac MPS port.

Upstream CLI (verified against the repo README):
    python generate.py IMAGE [--seed N] [--output NAME] [--pipeline-type 512|1024|1024_cascade]
                             [--texture-size 512|1024|2048] [--no-texture]
Writes NAME.glb with embedded base-color / metallic / roughness.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .base import (
    Backend,
    BackendError,
    EventFn,
    GenerateRequest,
    GenerateResult,
    claim_output,
    newest_glb,
    run_streaming,
)

DINO_REPO = "facebook/dinov3-vitl16-pretrain-lvd1689m"
DINO_PAGE = f"https://huggingface.co/{DINO_REPO}"


def _hf_token() -> str:
    for p in (
        Path.home() / ".cache/huggingface/token",
        Path.home() / ".huggingface/token",
    ):
        if p.exists():
            text = p.read_text().strip()
            if text:
                return text
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""


def _hub_has_weights(repo: str) -> bool:
    slug = "models--" + repo.replace("/", "--")
    root = Path.home() / ".cache/huggingface/hub" / slug
    if not root.exists():
        return False
    for p in root.rglob("*"):
        if p.suffix.lower() in {".safetensors", ".bin", ".pt", ".ckpt"} and p.stat().st_size > 1_000_000:
            return True
    return False


def _hf_can_read(repo: str, filename: str = "config.json") -> bool:
    token = _hf_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    try:
        with httpx.Client(follow_redirects=True, timeout=6.0) as client:
            r = client.get(url, headers=headers)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


class TrellisBackend(Backend):
    id = "trellis"
    name = "TRELLIS 2"
    kind = "local"
    blurb = "MIT weights, 4B DiT, up to 1536-cubed. Best all-round local geometry."
    max_images = 1
    accepts_text = False

    def available(self) -> tuple[bool, str]:
        root = settings.plumbus_trellis_path
        py = settings.plumbus_trellis_python
        if not root.exists():
            return False, f"not installed: {root} missing. Run `plumbus install trellis`."
        if not (root / "generate.py").exists():
            return False, f"{root}/generate.py missing. Checkout looks incomplete."
        if not py.exists():
            return False, f"no venv at {py}. Run setup.sh inside {root}."
        if not _hub_has_weights(DINO_REPO) and not _hf_can_read(DINO_REPO):
            return False, (
                f"DINOv3 is gated. Open {DINO_PAGE}, submit Meta's access form, "
                f"wait for approval, then retry. Until then use Hunyuan3D."
            )
        return True, ""

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "pipeline_type",
                "label": "Geometry resolution",
                "type": "select",
                "default": "1024",
                "options": [
                    {"value": "512", "label": "512  (fast, ~2 min)"},
                    {"value": "1024", "label": "1024  (sharp, ~5 min)"},
                    {"value": "1024_cascade", "label": "1024 cascade  (max detail, ~9 min)"},
                ],
                "help": "Voxel grid the structure stage solves on. The single biggest quality lever.",
            },
            {
                "key": "texture_size",
                "label": "Texture size",
                "type": "select",
                "default": "2048",
                "options": [
                    {"value": "512", "label": "512"},
                    {"value": "1024", "label": "1024"},
                    {"value": "2048", "label": "2048"},
                ],
            },
            {
                "key": "no_texture",
                "label": "Geometry only",
                "type": "bool",
                "default": False,
                "help": "Skip PBR baking. Much faster when you only need the mesh.",
            },
            {
                "key": "seed",
                "label": "Seed",
                "type": "int",
                "default": 42,
                "min": 0,
                "max": 2**31 - 1,
                "help": "Same seed and same image gives the same mesh.",
            },
        ]

    async def generate(self, req: GenerateRequest, on_event: EventFn) -> GenerateResult:
        ok, reason = self.available()
        if not ok:
            raise BackendError(reason)
        if not req.images:
            raise BackendError("TRELLIS 2 needs one source image.")

        root = settings.plumbus_trellis_path
        p = req.params
        out_stem = req.out_dir / req.job_id

        cmd = [
            str(settings.plumbus_trellis_python),
            "generate.py",
            str(req.images[0].resolve()),
            "--output", str(out_stem.resolve()),
            "--seed", str(int(p.get("seed", 42))),
            "--pipeline-type", str(p.get("pipeline_type", "1024")),
            "--texture-size", str(p.get("texture_size", "2048")),
        ]
        if p.get("no_texture"):
            cmd.append("--no-texture")

        started = time.time()
        await on_event("starting", 0.0, "$ " + " ".join(cmd))
        try:
            await run_streaming(
                cmd,
                cwd=root,
                on_event=on_event,
                env={
                    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                    "SPARSE_CONV_BACKEND": "flex_gemm",
                },
                stage_hint="structure",
            )
        except BackendError as exc:
            msg = str(exc).lower()
            if "dinov3" in msg or "gated repo" in msg:
                raise BackendError(
                    "TRELLIS needs gated DINOv3 access. "
                    f"Open {DINO_PAGE}, submit Meta's form, then retry. Use Hunyuan3D until then."
                ) from exc
            raise

        glb = out_stem.with_suffix(".glb")
        if not glb.exists():
            # generate.py has moved its output convention before. Fall back to a scan.
            found = newest_glb(req.out_dir, root, after=started - 1)
            if not found:
                raise BackendError(
                    f"run finished but no .glb appeared at {glb} or under {root}"
                )
            glb = claim_output(found, req.out_dir, req.job_id)

        return GenerateResult(
            glb_path=glb,
            cost_usd=0.0,
            stats={
                "engine": "trellis-2",
                "pipeline_type": p.get("pipeline_type", "1024"),
                "texture_size": None if p.get("no_texture") else p.get("texture_size", "2048"),
                "seconds": round(time.time() - started, 1),
            },
        )
