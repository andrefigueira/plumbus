"""Drive the headless-Blender conform stage: scan in, riggable head out.

The scan is the mould, the template is the asset. blender/conform_head.py does
the geometry; this module finds Blender, runs it, and verifies the output
actually carries what the whole feature exists for: independent eye meshes and
surviving morph targets.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import ROOT

BLENDER_CANDIDATES = (
    os.environ.get("PLUMBUS_BLENDER", ""),
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "blender",
)

DEFAULT_TEMPLATE = ROOT / "templates" / "basehead-stub.glb"
CONFORM_SCRIPT = ROOT / "blender" / "conform_head.py"


def find_blender() -> str | None:
    for cand in BLENDER_CANDIDATES:
        if not cand:
            continue
        if Path(cand).is_file() or shutil.which(cand):
            return cand
    return None


def conform_head(
    scan: Path,
    out: Path,
    template: Path | None = None,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Returns the conform report, with ok=False and a reason on any failure."""
    blender = find_blender()
    if not blender:
        return {"ok": False, "error": "Blender not found. Install it, or set PLUMBUS_BLENDER."}
    tpl = Path(template or DEFAULT_TEMPLATE)
    if not tpl.is_file():
        return {"ok": False, "error": f"no template at {tpl}. Build the stub with "
                f"`Blender --background --python blender/make_template_stub.py -- --out {tpl}`"}
    if not Path(scan).is_file():
        return {"ok": False, "error": f"no scan at {scan}"}

    proc = subprocess.run(
        [blender, "--background", "--python", str(CONFORM_SCRIPT), "--",
         "--scan", str(scan), "--template", str(tpl), "--out", str(out)],
        capture_output=True, text=True, timeout=timeout_s,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PLUMBUS_RESULT:")), None)
    if not line:
        return {"ok": False, "error": f"Blender gave no result "
                f"(exit {proc.returncode}): {proc.stdout[-400:] or proc.stderr[-400:]}"}
    report: dict[str, Any] = json.loads(line.split(":", 1)[1])
    if not report.get("ok"):
        return report

    # Trust nothing: the output must really carry the anatomy.
    from .postprocess import mesh_stats, production_stats

    stats = production_stats(Path(out))
    shape = mesh_stats(Path(out))
    report["verified"] = {
        "morph_targets": stats.get("morph_targets", 0),
        "meshes": shape.get("meshes", 0),
    }
    if not stats.get("morph_targets"):
        report["ok"] = False
        report["error"] = "conform ran but the output lost its morph targets"
    if (shape.get("meshes") or 0) < 2:
        report["ok"] = False
        report["error"] = "conform ran but the eyes are not independent meshes"
    return report
