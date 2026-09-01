"""Backend contract.

Every engine (local or hosted) implements this. The web UI renders its whole
parameter form from `param_schema()`, so adding an engine needs no frontend work.
"""
from __future__ import annotations

import abc
import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

EventFn = Callable[[str, float | None, str | None], Awaitable[None]]
"""on_event(stage, progress_0_to_1 | None, log_line | None)"""


@dataclass
class GenerateRequest:
    job_id: str
    images: list[Path]
    out_dir: Path
    prompt: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerateResult:
    glb_path: Path
    cost_usd: float | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class BackendError(RuntimeError):
    pass


class Backend(abc.ABC):
    id: str
    name: str
    kind: str  # "local" | "hosted"
    blurb: Any = ""  # str, or a property on the subclass
    max_images: int = 1
    accepts_text: bool = False

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable_right_now, human readable reason if not)"""

    def param_schema(self) -> list[dict[str, Any]]:
        return []

    def preflight(self, params: dict[str, Any]) -> tuple[bool, str]:
        """Validate a *specific* settings combination before anything is queued.

        `available()` answers "is this engine installed". This answers "will these
        settings actually work", which is a different question and the one that
        used to be answered 33 minutes into a run.
        """
        return True, ""

    def image_limit(self, params: dict[str, Any]) -> int:
        """Maximum sources for this exact configuration."""
        return self.max_images

    @abc.abstractmethod
    async def generate(self, req: GenerateRequest, on_event: EventFn) -> GenerateResult: ...


# ---------------------------------------------------------------- subprocess

_PROGRESS_RE = re.compile(r"(\d{1,3})\s*%")
_STAGE_RE = re.compile(r"^\s*\[?(sparse|structure|latent|mesh|decode|bake|texture|export|rembg|preprocess)[\w \-]*\]?", re.I)


async def run_streaming(
    cmd: list[str],
    cwd: Path,
    on_event: EventFn,
    env: dict[str, str] | None = None,
    stage_hint: str = "generating",
) -> None:
    """Run a child process, forwarding stdout to the job log and guessing progress.

    Local engines print free-form logs. We scrape a percentage when one appears
    and fall back to stage names, which is enough to keep the UI honest.
    """
    full_env = {**os.environ, **(env or {})}
    full_env.setdefault("PYTHONUNBUFFERED", "1")

    # tqdm writes \r updates with no newline. asyncio.readline() then buffers
    # until 64KB and raises ValueError("Separator is not found...").
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=8 * 1024 * 1024,
    )
    assert proc.stdout is not None

    stage = stage_hint
    tail: list[str] = []
    buf = b""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            if buf.strip():
                chunk = b"\n"
            else:
                break
        buf += chunk
        while True:
            n = buf.find(b"\n")
            r = buf.find(b"\r")
            if n < 0 and r < 0:
                if len(buf) > 256 * 1024:
                    buf = buf[-4096:]
                break
            if n < 0:
                idx, skip = r, 1
            elif r < 0 or n <= r:
                idx, skip = n, 1
            else:
                idx, skip = r, 2 if n == r + 1 else 1
            line = buf[:idx].decode("utf-8", "replace").rstrip()
            buf = buf[idx + skip :]
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            progress = None
            if m := _PROGRESS_RE.search(line):
                pct = int(m.group(1))
                if 0 <= pct <= 100:
                    progress = pct / 100
            if m := _STAGE_RE.match(line):
                stage = m.group(1).lower()
            await on_event(stage, progress, line)
        if not chunk:
            break

    rc = await proc.wait()
    if rc != 0:
        raise BackendError(f"{Path(cmd[0]).name} exited {rc}\n" + "\n".join(tail[-15:]))


def newest_glb(*dirs: Path, after: float = 0.0) -> Path | None:
    """Local engines vary on where they drop the GLB. Find the freshest one."""
    best: tuple[float, Path] | None = None
    for d in dirs:
        if not d or not d.exists():
            continue
        for p in d.rglob("*.glb"):
            mt = p.stat().st_mtime
            if mt >= after and (best is None or mt > best[0]):
                best = (mt, p)
    return best[1] if best else None


def claim_output(src: Path, out_dir: Path, job_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{job_id}.glb"
    shutil.copy2(src, dest)
    return dest
