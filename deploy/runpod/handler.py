"""Plumbus GPU worker for RunPod Serverless.

One endpoint, both engines. Invokes them exactly the way the local adapters do,
so a mesh generated in the cloud matches one generated on a laptop for the same
seed and settings. Weights live on the network volume, never in the image.

Input:
  {
    "engine":  "trellis" | "hunyuan",
    "image":   "<base64 png, with or without a data: prefix>",
    "params":  { ...engine specific, same keys the UI sends... }
  }

Output:
  { "glb_b64": "...", "stats": {...}, "log": [...] }
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import runpod

VOLUME = Path(os.environ.get("PLUMBUS_VOLUME", "/runpod-volume"))
ENGINE_ROOT = Path(os.environ.get("PLUMBUS_ENGINE_ROOT", "/opt/engines"))

# Anything the model downloads lands on the volume, so a warm worker never
# refetches and a cold one pays the download once for the endpoint's lifetime.
for var, sub in [
    ("HF_HOME", "hf"),
    ("HUGGINGFACE_HUB_CACHE", "hf/hub"),
    ("TORCH_HOME", "torch"),
    ("HY3DGEN_MODELS", "hy3dgen"),
]:
    p = VOLUME / sub
    p.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(var, str(p))

MAX_GLB_BYTES = 18 * 1024 * 1024  # RunPod's JSON response ceiling, with headroom


def _decode_image(raw: str, dest: Path) -> Path:
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    dest.write_bytes(base64.b64decode(raw))
    return dest


def _run(cmd: list[str], cwd: Path, log: list[str], env: dict | None = None) -> None:
    full = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=full,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        if line:
            log.append(line)
            print(line, flush=True)
            del log[:-400]
    if proc.wait() != 0:
        raise RuntimeError(f"{Path(cmd[1]).name} exited {proc.returncode}\n" + "\n".join(log[-15:]))


def _trellis(img: Path, out_stem: Path, p: dict, log: list[str]) -> Path:
    root = ENGINE_ROOT / "trellis"
    cmd = [
        sys.executable, "generate.py", str(img),
        "--output", str(out_stem),
        "--seed", str(int(p.get("seed", 42))),
        "--pipeline-type", str(p.get("pipeline_type", "1024")),
        "--texture-size", str(p.get("texture_size", "2048")),
    ]
    if p.get("no_texture"):
        cmd.append("--no-texture")
    _run(cmd, root, log)
    return out_stem.with_suffix(".glb")


def _hunyuan(img: Path, out: Path, p: dict, log: list[str]) -> Path:
    root = ENGINE_ROOT / "hunyuan"
    cmd = [
        sys.executable, str(ENGINE_ROOT / "hunyuan_runner.py"),
        "--image", str(img), "--output", str(out),
        "--steps", str(int(p.get("steps", 50))),
        "--guidance-scale", str(float(p.get("guidance_scale", 5.0))),
        "--octree-resolution", str(int(p.get("octree_resolution", 384))),
        "--max-faces", str(int(p.get("max_faces", 200000))),
        "--seed", str(int(p.get("seed", 42))),
    ]
    if p.get("texture", True):
        # On CUDA the painter genuinely works, so a failure here is a real fault.
        cmd += ["--texture", "--strict-texture"]
    _run(cmd, root, log)
    return out


def _stats(glb: Path) -> dict:
    out: dict = {"bytes": glb.stat().st_size}
    try:
        import trimesh

        scene = trimesh.load(glb, force="scene")
        geoms = list(scene.geometry.values())
        out["vertices"] = int(sum(len(g.vertices) for g in geoms))
        out["faces"] = int(sum(len(g.faces) for g in geoms))
        out["pbr"] = any(
            getattr(getattr(getattr(g, "visual", None), "material", None),
                    "metallicRoughnessTexture", None) is not None
            for g in geoms
        )
    except Exception as exc:  # noqa: BLE001
        out["stats_error"] = f"{exc.__class__.__name__}: {exc}"
    return out


def handler(job: dict) -> dict:
    started = time.time()
    log: list[str] = []
    inp = job.get("input") or {}

    engine = (inp.get("engine") or "trellis").lower()
    if engine not in ("trellis", "hunyuan"):
        return {"error": f"unknown engine {engine!r}"}
    if not inp.get("image"):
        return {"error": "no input image"}

    params = inp.get("params") or {}

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            img = _decode_image(inp["image"], tmp / "ref.png")
            log.append(f"[worker] engine={engine} params={json.dumps(params)}")

            glb = (_trellis(img, tmp / "out", params, log) if engine == "trellis"
                   else _hunyuan(img, tmp / "out.glb", params, log))

            if not glb.exists():
                return {"error": "engine finished but produced no .glb", "log": log[-40:]}

            size = glb.stat().st_size
            if size > MAX_GLB_BYTES:
                return {
                    "error": f"mesh is {size // 1024 // 1024}MB, over the "
                             f"{MAX_GLB_BYTES // 1024 // 1024}MB inline limit. "
                             f"Lower target_polycount or texture_size.",
                    "log": log[-40:],
                }

            stats = {**_stats(glb), "engine": engine, "seconds": round(time.time() - started, 1)}
            return {
                "glb_b64": base64.b64encode(glb.read_bytes()).decode(),
                "stats": stats,
                "log": log[-60:],
            }

    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{exc.__class__.__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
            "log": log[-60:],
        }


runpod.serverless.start({"handler": handler})
