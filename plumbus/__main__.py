"""Plumbus CLI.

    plumbus serve                 start the UI
    plumbus doctor                what is installed, what is not
    plumbus install trellis       clone + set up a local engine
    plumbus gen ref.png           one-shot generate without the UI
    plumbus mcp                   MCP server over stdio, for agents
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from . import backends, db
from .config import ROOT, settings

def _engine_paths(engine: str):
    """Where this engine lives on this platform."""
    from .hardware import spec_for
    spec = spec_for(engine)
    if not spec:
        return None, None, None
    root = {
        "trellis": settings.plumbus_trellis_path,
        "hunyuan": settings.plumbus_hunyuan_path,
    }[engine]
    return spec, root, root / spec["venv"]


def cmd_doctor(_: argparse.Namespace) -> int:
    from .hardware import device_of, os_name, summary

    print(f"plumbus  root={ROOT}")
    print(f"         python={sys.version.split()[0]}  db={settings.plumbus_db_path.name}")
    print(f"         machine={summary()}")
    print()
    local_ok = False
    for info in backends.describe_all():
        mark = "ok  " if info["available"] else "MISS"
        if info["available"] and info["kind"] == "local":
            local_ok = True
        print(f"  [{mark}] {info['name']:<18} ({info['kind']})")
        print(f"         {info['blurb']}")
        if info["kind"] == "local":
            spec, _, venv = _engine_paths(info["id"])
            if spec:
                dev = device_of(str(venv))
                tex = "textures: yes" if spec["textures"] else "textures: NO on this platform"
                print(f"         device={dev}  {tex}")
        if info["reason"]:
            print(f"         -> {info['reason']}")
    print()
    if not local_ok:
        if settings.plumbus_trellis_path.exists():
            print("TRELLIS is installed but not usable yet. See the reason above.")
        else:
            print("Install a local engine with:  plumbus install trellis")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from .hardware import ENGINE_SPECS, os_name

    spec, dest, _ = _engine_paths(args.engine)
    if not spec:
        print(f"no {args.engine!r} build for {os_name()}. "
              f"Known engines: {', '.join(ENGINE_SPECS)}")
        return 2

    print(f"engine : {args.engine}  ({os_name()})")
    print(f"source : {spec['repo']}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{spec['note']}\n")
    if not args.yes:
        if input(f"Clone {spec['repo']} into {dest}? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 1

    if dest.exists():
        print(f"{dest} already exists, skipping clone")
    else:
        subprocess.run(["git", "clone", "--depth", "1", spec["repo"], str(dest)], check=True)

    if spec["setup"]:
        print(f"\nRunning {' '.join(spec['setup'])} in {dest}. This downloads the weights.")
        subprocess.run(spec["setup"], cwd=dest, check=True)
        if args.engine == "trellis":
            _ensure_trellis_ovoxel(dest)
            _patch_trellis_rembg(dest)
            _prefetch_trellis_weights(dest)
    else:
        if args.engine == "hunyuan":
            _install_hunyuan(dest)
        else:
            print("\nNo automated setup for this engine. Follow the note above, then run `plumbus doctor`.")
    return 0


def _ensure_trellis_ovoxel(dest: Path) -> None:
    """trellis-mac clones trellis2-apple without submodules, so Eigen is empty
    and o_voxel's Metal baker fails to compile. Fetch it and retry the install."""
    apple = dest / "deps" / "trellis2-apple"
    py = dest / ".venv" / "bin" / "python"
    ovoxel = apple / "o-voxel"
    if not apple.exists() or not py.exists() or not ovoxel.exists():
        return

    probe = subprocess.run([str(py), "-c", "import o_voxel"], capture_output=True)
    if probe.returncode == 0:
        return

    print("o_voxel missing (Eigen submodule was empty). Fetching it and retrying the Metal baker.")
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
        cwd=apple,
        check=False,
    )
    env = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": "12.0"}
    retry = subprocess.run(
        ["uv", "pip", "install", "--python", str(py), "--no-build-isolation", str(ovoxel)],
        env=env,
    )
    if retry.returncode == 0:
        print("o_voxel Metal baker installed.")
    else:
        print("o_voxel still failed to build. TRELLIS will use the slower KDTree baker.")


def _patch_trellis_rembg(dest: Path) -> None:
    """RMBG-2.0 is gated + non-commercial. Fall back to ungated BiRefNet."""
    needle = (
        "        pipeline.rembg_model = getattr(rembg, args['rembg_model']['name'])"
        "(**args['rembg_model']['args'])\n"
    )
    replacement = (
        "        try:\n"
        "            pipeline.rembg_model = getattr(rembg, args['rembg_model']['name'])"
        "(**args['rembg_model']['args'])\n"
        "        except Exception as rembg_exc:\n"
        '            print(f"RMBG unavailable ({rembg_exc}); falling back to ZhengPeng7/BiRefNet")\n'
        '            pipeline.rembg_model = rembg.BiRefNet(model_name="ZhengPeng7/BiRefNet")\n'
    )
    patched = False
    for rel in (
        "TRELLIS.2/trellis2/pipelines/trellis2_image_to_3d.py",
        "TRELLIS.2/trellis2/pipelines/trellis2_texturing.py",
    ):
        path = dest / rel
        if not path.exists():
            continue
        src = path.read_text()
        if "ZhengPeng7/BiRefNet" in src:
            continue
        if needle not in src:
            continue
        path.write_text(src.replace(needle, replacement, 1))
        patched = True
    if patched:
        print("Patched TRELLIS rembg to fall back to ungated BiRefNet.")


def _prefetch_trellis_weights(dest: Path) -> None:
    """setup.sh does not pull weights. First generate would otherwise stall on ~15GB."""
    py = dest / ".venv" / "bin" / "python"
    if not py.exists():
        return
    print("\nPrefetching TRELLIS.2 and BiRefNet weights. DINOv3 is gated and needs Hub approval.")
    code = (
        "from huggingface_hub import snapshot_download\n"
        "from huggingface_hub.errors import GatedRepoError\n"
        "print('  downloading microsoft/TRELLIS.2-4B', flush=True)\n"
        "snapshot_download('microsoft/TRELLIS.2-4B')\n"
        "print('  downloading ZhengPeng7/BiRefNet', flush=True)\n"
        "snapshot_download('ZhengPeng7/BiRefNet')\n"
        "print('  downloading facebook/dinov3-vitl16-pretrain-lvd1689m', flush=True)\n"
        "try:\n"
        "    snapshot_download('facebook/dinov3-vitl16-pretrain-lvd1689m')\n"
        "except GatedRepoError:\n"
        "    print('DINOv3 is gated. Open https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m')\n"
        "    print('Submit Meta\\'s access form, wait for approval, then rerun plumbus doctor.')\n"
        "print('weights cached')\n"
    )
    subprocess.run([str(py), "-c", code], check=False)


def _install_hunyuan(dest: Path) -> None:
    """Clone is done. Create the venv plumbus looks for and install macOS deps."""
    venv = dest / ".venv"
    py = venv / "bin" / "python"
    if not venv.exists():
        print("Creating Hunyuan venv (Python 3.12)...")
        subprocess.run(["uv", "venv", str(venv), "--python", "python3.12"], check=True)
    print("Installing Hunyuan torch + requirements-macos.txt...")
    subprocess.run(
        [
            "uv", "pip", "install", "--python", str(py),
            "torch==2.5.1", "torchvision==0.20.1", "torchaudio==2.5.1",
        ],
        check=True,
    )
    req = dest / "requirements-macos.txt"
    if req.exists():
        subprocess.run(
            ["uv", "pip", "install", "--python", str(py), "-r", str(req)],
            check=False,
        )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(py), "trimesh", "pymeshlab"],
        check=False,
    )
    ckpt = dest / "hy3dpaint" / "ckpt" / "RealESRGAN_x4plus.pth"
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    if not ckpt.exists():
        print("Downloading RealESRGAN_x4plus.pth...")
        subprocess.run(
            [
                "curl", "-L",
                "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
                "-o", str(ckpt),
            ],
            check=False,
        )
    _patch_hunyuan_torch_load(dest)
    print("Hunyuan setup finished. Run `plumbus doctor` to confirm.")


def _patch_hunyuan_torch_load(dest: Path) -> None:
    """torch 2.5 weights_only=True cannot unpickle Hunyuan's zip ckpt."""
    path = dest / "hy3dshape" / "hy3dshape" / "pipelines.py"
    if not path.exists():
        return
    src = path.read_text()
    if "weights_only=True" not in src:
        return
    path.write_text(src.replace("weights_only=True", "weights_only=False"))
    print("Patched Hunyuan torch.load to weights_only=False.")


def cmd_gen(args: argparse.Namespace) -> int:
    db.init()
    images = [Path(p) for p in args.images]
    for p in images:
        if not p.exists():
            print(f"no such image: {p}")
            return 2

    params = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        params[k] = v

    backend = backends.get(args.backend or settings.plumbus_default_backend)
    ok, reason = backend.available()
    if not ok and not args.backend:
        fallback = next((b for b in backends.REGISTRY.values() if b.available()[0]), None)
        if fallback:
            print(f"{backend.name} unavailable; using {fallback.name}")
            backend = fallback
            ok, reason = True, ""
    if not ok:
        print(f"{backend.name} unavailable: {reason}")
        return 1

    job_id = db.create_job(backend.id, [str(p) for p in images], params)
    db.update(job_id, status="running", started_at=time.time())
    print(f"job {job_id} on {backend.name}")

    async def on_event(stage, progress, line):
        if line:
            print(f"  {line}")
        elif progress is not None:
            print(f"  [{stage}] {progress:.0%}")

    req = backends.GenerateRequest(
        job_id=job_id, images=images, out_dir=settings.output_dir, params=params
    )
    started = time.time()
    try:
        result = asyncio.run(backend.generate(req, on_event))
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: {exc}")
        db.update(job_id, status="failed", error=str(exc))
        return 1

    from .postprocess import mesh_stats

    stats = {**result.stats, **mesh_stats(result.glb_path)}
    db.update(job_id, status="done", glb_path=str(result.glb_path), stats=stats, progress=1.0)
    print(f"\n{result.glb_path}")
    print(f"  {stats.get('vertices', '?')} verts / {stats.get('faces', '?')} faces "
          f"/ {round(stats.get('bytes', 0) / 1024)}KB in {round(time.time() - started)}s")
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    # stdio transport: the protocol owns stdout, so nothing here may print to it.
    from .mcp_server import run

    run()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    host, port = args.host or settings.plumbus_host, args.port or settings.plumbus_port
    print(f"\n  plumbus  ->  http://{host}:{port}\n")
    uvicorn.run("plumbus.server:app", host=host, port=port, reload=args.reload, log_level="info")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="plumbus", description="Local-first image-to-3D.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve)

    d = sub.add_parser("doctor", help="show which engines are usable")
    d.set_defaults(fn=cmd_doctor)

    i = sub.add_parser("install", help="clone and set up a local engine")
    i.add_argument("engine", choices=["trellis", "hunyuan"])
    i.add_argument("-y", "--yes", action="store_true")
    i.set_defaults(fn=cmd_install)

    m = sub.add_parser("mcp", help="MCP server over stdio (needs `plumbus serve` running)")
    m.set_defaults(fn=cmd_mcp)

    g = sub.add_parser("gen", help="generate one asset, no UI")
    g.add_argument("images", nargs="+")
    g.add_argument("-b", "--backend")
    g.add_argument("--set", action="append", metavar="KEY=VALUE")
    g.set_defaults(fn=cmd_gen)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
