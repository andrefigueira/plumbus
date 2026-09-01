"""Runner executed *inside* the Hunyuan3D-2.1 checkout, using its own interpreter.

The macOS fork ships a Gradio app but no batch CLI, so Plumbus drives the Python
API directly. Kept dependency-free beyond what that checkout already installs.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path


def log(stage: str, msg: str) -> None:
    print(f"[{stage}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--output", required=True, help="output .glb path")
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--guidance-scale", type=float, default=5.0)
    ap.add_argument("--octree-resolution", type=int, default=256)
    ap.add_argument("--num-chunks", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-faces", type=int, default=40000)
    ap.add_argument("--texture", action="store_true")
    ap.add_argument("--texture-size", type=int, default=1024)
    ap.add_argument("--no-rembg", action="store_true")
    ap.add_argument("--strict-texture", action="store_true",
                    help="Fail loudly if texturing fails instead of exporting a grey mesh.")
    args = ap.parse_args()

    root = Path(os.environ.get("HUNYUAN_ROOT") or os.getcwd()).resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "hy3dshape"))
    sys.path.insert(0, str(root / "hy3dpaint"))
    os.chdir(root)

    try:
        from torchvision_fix import apply_fix
        apply_fix()
    except Exception as exc:  # noqa: BLE001
        log("preprocess", f"torchvision_fix skipped ({exc.__class__.__name__})")

    import torch

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    log("preprocess", f"device={device} torch={torch.__version__}")

    from PIL import Image

    image = Image.open(args.image).convert("RGBA")

    if not args.no_rembg:
        try:
            from hy3dshape.rembg import BackgroundRemover

            log("rembg", "removing background")
            image = BackgroundRemover()(image)
        except Exception as exc:  # noqa: BLE001
            log("rembg", f"skipped ({exc.__class__.__name__}: {exc})")

    from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline

    log("structure", "loading shape pipeline tencent/Hunyuan3D-2.1")
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained("tencent/Hunyuan3D-2.1")
    try:
        pipe.to(device)
    except Exception as exc:  # noqa: BLE001
        log("structure", f"stayed on default device ({exc})")

    log("mesh", f"sampling steps={args.steps} octree={args.octree_resolution}")
    generator = torch.Generator().manual_seed(args.seed)
    mesh = pipe(
        image=image,
        num_inference_steps=args.steps,
        guidance_scale=args.guidance_scale,
        octree_resolution=args.octree_resolution,
        num_chunks=args.num_chunks,
        generator=generator,
        output_type="trimesh",
    )[0]
    log("mesh", f"raw mesh: {len(mesh.vertices)} verts / {len(mesh.faces)} faces")

    try:
        from hy3dshape.postprocessors import FaceReducer, FloaterRemover, DegenerateFaceRemover

        log("mesh", "cleaning: floaters, degenerate faces, decimate")
        mesh = FloaterRemover()(mesh)
        mesh = DegenerateFaceRemover()(mesh)
        mesh = FaceReducer()(mesh, max_facenum=args.max_faces)
        log("mesh", f"cleaned mesh: {len(mesh.vertices)} verts / {len(mesh.faces)} faces")
    except Exception as exc:  # noqa: BLE001
        log("mesh", f"postprocess skipped ({exc.__class__.__name__}: {exc})")

    if args.texture:
        try:
            from hy3dpaint.textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline

            log("texture", f"PBR paint at {args.texture_size}px")
            tmp_obj = args.output.rsplit(".", 1)[0] + "_shape.obj"
            mesh.export(tmp_obj)
            conf = Hunyuan3DPaintConfig(max_num_view=6, resolution=512)
            painter = Hunyuan3DPaintPipeline(conf)
            painted = painter(mesh_path=tmp_obj, image_path=args.image)
            import trimesh

            mesh = trimesh.load(painted, force="mesh")
        except Exception as exc:  # noqa: BLE001
            msg = f"{exc.__class__.__name__}: {exc}"
            if args.strict_texture:
                log("texture", f"FAILED: {msg}")
                raise SystemExit(
                    f"Texturing failed and --strict-texture is set, so the mesh was not "
                    f"exported as untextured.\n  cause: {msg}\n"
                    f"  Hunyuan's painter needs CUDA (custom_rasterizer is a CUDAExtension). "
                    f"On Apple Silicon use TRELLIS 2 for textures."
                )
            log("texture", f"FAILED, exporting untextured ({msg})")

    log("export", f"writing {args.output}")
    mesh.export(args.output)
    log("export", "done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
