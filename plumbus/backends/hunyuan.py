"""Hunyuan3D 2.1 on Apple Silicon via the Brainkeys MPS fork.

The fork exposes a Gradio app rather than a batch CLI, so we drive its Python
API through a bundled runner script executed with the checkout's interpreter.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}

from ..config import settings
from ..hardware import spec_for, supports_cuda_extensions
from .base import (
    Backend,
    BackendError,
    EventFn,
    GenerateRequest,
    GenerateResult,
    run_streaming,
)

RUNNER = Path(__file__).parent / "runners" / "hunyuan_runner.py"

def _cuda_available() -> bool:
    return supports_cuda_extensions(str(settings.plumbus_hunyuan_python))


class HunyuanBackend(Backend):
    id = "hunyuan"
    name = "Hunyuan3D 2.1"
    kind = "local"
    @property
    def blurb(self) -> str:  # type: ignore[override]
        spec = spec_for("hunyuan")
        if spec and not spec["textures"]:
            return "Open weights. Geometry only here: the PBR painter is CUDA-only."
        return "Open weights, production PBR texture synthesis. Strongest local texturing."
    max_images = 1
    accepts_text = False

    def available(self) -> tuple[bool, str]:
        root = settings.plumbus_hunyuan_path
        py = settings.plumbus_hunyuan_python
        if not root.exists():
            return False, f"not installed: {root} missing. Run `plumbus install hunyuan`."
        if not py.exists():
            return False, f"no venv at {py}. Create one inside {root}."
        return True, ""

    def preflight(self, params: dict[str, Any]) -> tuple[bool, str]:
        if not params.get("texture", True):
            return True, ""
        if _cuda_available():
            return True, ""
        return False, (
            "PBR texturing needs CUDA. Hunyuan's painter depends on custom_rasterizer, "
            "a CUDAExtension that cannot build on Apple Silicon, so this run would "
            "spend the full shape time and then export an untextured mesh. "
            "Either turn PBR texturing off, or use TRELLIS 2 which bakes PBR natively on MPS."
        )

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "octree_resolution",
                "label": "Octree resolution",
                "type": "select",
                "default": "384",
                "options": [
                    {"value": "128", "label": "128  (draft)"},
                    {"value": "256", "label": "256  (default)"},
                    {"value": "384", "label": "384  (fine)"},
                    {"value": "512", "label": "512  (slow, hungry)"},
                ],
                "help": "Marching-cubes grid for mesh extraction. 512 will use most of your 64GB.",
            },
            {
                "key": "steps",
                "label": "Diffusion steps",
                "type": "int",
                "default": 50,
                "min": 10,
                "max": 100,
                "help": "Past about 50 the returns flatten out.",
            },
            {
                "key": "guidance_scale",
                "label": "Guidance",
                "type": "float",
                "default": 5.0,
                "min": 1.0,
                "max": 15.0,
                "step": 0.5,
                "help": "How hard it clings to the reference image.",
            },
            {
                "key": "max_faces",
                "label": "Face budget",
                "type": "int",
                "default": 200000,
                "min": 2000,
                "max": 500000,
                "help": "Decimation target after cleanup. 150k-350k preserves hero detail; use the optimised web copy for delivery.",
            },
            {
                "key": "texture",
                "label": "PBR texturing",
                "type": "bool",
                "default": False,
                "help": "Adds a lot of time and memory. Off gives a grey mesh. Keep off until a first generate succeeds.",
            },
            {
                "key": "seed",
                "label": "Seed",
                "type": "int",
                "default": 42,
                "min": 0,
                "max": 2**31 - 1,
            },
        ]

    async def generate(self, req: GenerateRequest, on_event: EventFn) -> GenerateResult:
        ok, reason = self.available()
        if not ok:
            raise BackendError(reason)
        if not req.images:
            raise BackendError("Hunyuan3D needs one source image.")

        p = req.params
        glb = (req.out_dir / f"{req.job_id}.glb").resolve()

        cmd = [
            str(settings.plumbus_hunyuan_python),
            str(RUNNER.resolve()),
            "--image", str(req.images[0].resolve()),
            "--output", str(glb),
            "--steps", str(int(p.get("steps", 50))),
            "--guidance-scale", str(float(p.get("guidance_scale", 5.0))),
            "--octree-resolution", str(int(p.get("octree_resolution", 384))),
            "--max-faces", str(int(p.get("max_faces", 200000))),
            "--seed", str(int(p.get("seed", 42))),
        ]
        if _as_bool(p.get("texture"), False):
            cmd.append("--texture")

        started = time.time()
        await on_event("starting", 0.0, "$ " + " ".join(cmd))
        try:
            await run_streaming(
                cmd,
                cwd=settings.plumbus_hunyuan_path,
                on_event=on_event,
                env={
                    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                    "HUNYUAN_ROOT": str(settings.plumbus_hunyuan_path),
                },
                stage_hint="structure",
            )
        except Exception as exc:
            if not glb.exists():
                raise BackendError(str(exc)) from exc
            await on_event("export", 0.9, f"{exc.__class__.__name__} during logs; GLB is present, keeping it")

        if not glb.exists():
            raise BackendError(f"run finished but {glb} was never written")

        return GenerateResult(
            glb_path=glb,
            cost_usd=0.0,
            stats={
                "engine": "hunyuan3d-2.1",
                "octree_resolution": p.get("octree_resolution", 384),
                "steps": p.get("steps", 50),
                "textured": _as_bool(p.get("texture"), False),
                "seconds": round(time.time() - started, 1),
            },
        )
