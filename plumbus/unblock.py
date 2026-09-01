"""Redirect TRELLIS.2's gated DINOv3 dependency to an open mirror.

TRELLIS.2 loads its image encoder by repo id from JSON configs:

    "model_name": "facebook/dinov3-vitl16-pretrain-lvd1689m"

That repo is gated=manual, so a first run dies with a 403 until Meta approves you
by hand. The identical weights are mirrored ungated, and `DINOv3ViTModel` loads
them the same way, so pointing the configs at a mirror unblocks local generation
immediately.

LICENCE WARNING
---------------
This routes around Meta's access gate. The weights are the same, the licence is
not waived. Fine for local development and evaluation. Before you ship anything
commercial, get real DINOv3 approval and revert this with `--revert`, or accept
that you are distributing a product built on weights you never accepted terms
for. Keep the access request in flight either way.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

GATED = "facebook/dinov3-vitl16-pretrain-lvd1689m"

# Ungated mirrors of the same checkpoint, most-downloaded first.
MIRRORS = [
    "camenduru/dinov3-vitl16-pretrain-lvd1689m",
    "visualbruno/dinov3-vitl16-pretrain-lvd1689m",
]

CONFIG_GLOBS = ["configs/**/*.json", "trellis2/**/*.json"]
SOURCE_ROOTS = ["TRELLIS.2", "deps/trellis2-apple"]


def _config_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for sub in SOURCE_ROOTS:
        base = root / sub
        if not base.exists():
            continue
        for pattern in CONFIG_GLOBS:
            out.extend(base.glob(pattern))
    return sorted(set(out))


def scan(root: Path) -> dict[str, list[Path]]:
    """Which configs currently point where."""
    hits: dict[str, list[Path]] = {}
    for f in _config_files(root):
        try:
            text = f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for repo in (GATED, *MIRRORS):
            if repo in text:
                hits.setdefault(repo, []).append(f)
    return hits


def apply(root: Path, mirror: str | None = None, revert: bool = False) -> list[tuple[Path, str, str]]:
    """Rewrite the encoder repo id. Returns (file, before, after) per change."""
    mirror = mirror or MIRRORS[0]
    frm, to = (mirror, GATED) if revert else (GATED, mirror)

    # Reverting should catch whichever mirror was applied, not just the default.
    sources = MIRRORS if revert else [GATED]
    changed: list[tuple[Path, str, str]] = []

    for f in _config_files(root):
        try:
            text = original = f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for src in sources:
            if src in text:
                text = text.replace(src, to)
        if text != original:
            # Never write a config we have just corrupted.
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{f} would become invalid JSON: {exc}") from None
            bak = f.with_suffix(f.suffix + ".plumbus-bak")
            if not bak.exists():
                bak.write_text(original)
            f.write_text(text)
            changed.append((f, frm, to))

    return changed
