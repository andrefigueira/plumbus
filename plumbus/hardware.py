"""Platform and accelerator detection, plus the per-platform engine catalogue.

Plumbus runs two places with different physics: a Mac on MPS, where texture
pipelines built on CUDA extensions simply cannot build, and a Linux box on CUDA,
where they can. Everything that differs between those two lives here so the
backends stay about the model and not about the machine.
"""
from __future__ import annotations

import functools
import platform as _stdlib_platform
import subprocess
from pathlib import Path
from typing import Any, Literal

OSName = Literal["darwin", "linux", "windows"]
Device = Literal["cuda", "mps", "cpu"]


def os_name() -> OSName:
    s = _stdlib_platform.system().lower()
    if s == "darwin":
        return "darwin"
    if s == "windows":
        return "windows"
    return "linux"


def cpu_label() -> str:
    if os_name() == "darwin":
        try:
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or _stdlib_platform.machine()
        except Exception:  # noqa: BLE001
            pass
    return _stdlib_platform.machine()


@functools.lru_cache(maxsize=16)
def device_of(python_exe: str) -> Device:
    """Ask a *specific* interpreter what it can accelerate on.

    Each engine has its own venv with its own torch build, so the answer for
    Plumbus's interpreter tells you nothing about the engine's. Cached because
    it costs a subprocess.
    """
    if not Path(python_exe).exists():
        return "cpu"
    probe = (
        "import torch;"
        "print('cuda' if torch.cuda.is_available() else "
        "('mps' if torch.backends.mps.is_available() else 'cpu'))"
    )
    try:
        out = subprocess.run(
            [python_exe, "-c", probe], capture_output=True, text=True, timeout=90
        )
        val = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "cpu"
        return val if val in ("cuda", "mps", "cpu") else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def supports_cuda_extensions(python_exe: str) -> bool:
    """Can this engine build CUDAExtension modules (custom_rasterizer and friends)?"""
    return device_of(python_exe) == "cuda"


# --------------------------------------------------------------------------
# Engine catalogue. One entry per engine per platform.
#
# The macOS entries are community MPS ports. The Linux entries are upstream,
# because on CUDA the original repos work as published and carry the texture
# pipelines the ports cannot build.
# --------------------------------------------------------------------------

ENGINE_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "trellis": {
        "darwin": {
            "repo": "https://github.com/shivampkumar/trellis-mac",
            "dirname": "trellis-mac",
            "setup": ["bash", "setup.sh"],
            "venv": ".venv/bin/python",
            "textures": True,
            "note": (
                "Apple Silicon MPS port of TRELLIS.2.\n"
                "Needs a HuggingFace login plus access to two gated repos:\n"
                "  facebook/dinov3-vitl16-pretrain-lvd1689m  (manual review by Meta)\n"
                "  briaai/RMBG-2.0                           (auto, one click)\n"
                "About 15GB of weights, downloaded on first generation.\n"
                "RMBG-2.0 is CC BY-NC 4.0. It is only the background remover, so feed it\n"
                "pre-cut transparent references and it is never invoked."
            ),
        },
        "linux": {
            "repo": "https://github.com/microsoft/TRELLIS",
            "dirname": "TRELLIS",
            "setup": ["bash", "setup.sh", "--new-env", "--basic", "--flash-attn"],
            "venv": ".venv/bin/python",
            "textures": True,
            "note": (
                "Upstream TRELLIS on CUDA. Confirm you are on the TRELLIS.2 release or\n"
                "branch, the mac port tracks .2 while this repo's default may be .1.\n"
                "Needs nvcc and a matching torch CUDA build."
            ),
        },
    },
    "hunyuan": {
        "darwin": {
            "repo": "https://github.com/Brainkeys/Hunyuan3D-2.1-mac",
            "dirname": "Hunyuan3D-2.1-mac",
            "setup": None,
            "venv": ".venv/bin/python",
            "textures": False,
            "note": (
                "Apple Silicon MPS port. GEOMETRY ONLY.\n"
                "The PBR painter depends on custom_rasterizer, a CUDAExtension compiling\n"
                "rasterizer_gpu.cu with no CPU or MPS path, so texturing cannot run here.\n"
                "Use TRELLIS 2 for textured meshes on a Mac.\n"
                "Set up manually: create a 3.11 or 3.12 venv in the checkout, then\n"
                "  pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1\n"
                "  pip install -r requirements-macos.txt trimesh pymeshlab"
            ),
        },
        "linux": {
            "repo": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1",
            "dirname": "Hunyuan3D-2.1",
            "setup": None,
            "venv": ".venv/bin/python",
            "textures": True,
            "note": (
                "Upstream Hunyuan3D 2.1 on CUDA, with the full PBR texture pipeline.\n"
                "After cloning, build the two extensions the painter needs:\n"
                "  pip install -r requirements.txt realesrgan\n"
                "  (cd hy3dpaint/custom_rasterizer && pip install -e .)\n"
                "  (cd hy3dpaint/DifferentiableRenderer && bash compile_mesh_painter.sh)\n"
                "Both need nvcc. This is the configuration where texturing actually works."
            ),
        },
    },
}


def spec_for(engine: str, os_override: str | None = None) -> dict[str, Any] | None:
    per_os = ENGINE_SPECS.get(engine)
    if not per_os:
        return None
    return per_os.get(os_override or os_name())


def summary() -> str:
    return f"{os_name()} / {cpu_label()}"
