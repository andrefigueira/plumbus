"""Hosted escape hatch via fal.ai's queue API.

Pay per call instead of per month. This exists for the handful of assets where a
local run is not good enough, not as the default path.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
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
)

QUEUE = "https://queue.fal.run"

# image_field: the input key each model wants its reference image under.
# cost_usd: published ballpark at time of writing; treat as an estimate, not a quote.
MODELS: dict[str, dict[str, Any]] = {
    "fal-ai/trellis": {
        "label": "TRELLIS (hosted)",
        "image_field": "image_url",
        "multi": False,
        "cost_usd": 0.02,
    },
    "fal-ai/hunyuan3d/v2": {
        "label": "Hunyuan3D v2",
        "image_field": "input_image_url",
        "multi": False,
        "cost_usd": 0.16,
    },
    "fal-ai/hunyuan3d/v2/multi-view": {
        "label": "Hunyuan3D v2 multi-view",
        "image_field": "front_image_url",
        "multi": True,
        "cost_usd": 0.20,
    },
    "fal-ai/hyper3d/rodin": {
        "label": "Rodin (Hyper3D)",
        "image_field": "input_image_urls",
        "multi": True,
        "cost_usd": 0.40,
    },
}


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _find_glb(payload: Any) -> str | None:
    """Walk an arbitrary JSON response for the first .glb URL."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and node.startswith("http") and ".glb" in node:
            return node
    return None


class FalBackend(Backend):
    id = "fal"
    name = "fal.ai (hosted)"
    kind = "hosted"
    blurb = "Per-call, no subscription. Use when a local run is not good enough."
    max_images = 4
    accepts_text = False

    def available(self) -> tuple[bool, str]:
        if not settings.fal_key:
            return False, "FAL_KEY not set. Add it to .env to enable the hosted fallback."
        return True, ""

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "model",
                "label": "Hosted model",
                "type": "select",
                "default": "fal-ai/trellis",
                "options": [
                    {"value": k, "label": f"{v['label']}  (~${v['cost_usd']:.2f}/gen)"}
                    for k, v in MODELS.items()
                ],
            },
            {
                "key": "extra_json",
                "label": "Extra input (JSON)",
                "type": "json",
                "default": {},
                "help": "Merged into the request body. For model-specific flags fal adds faster than this UI does.",
            },
        ]

    def image_limit(self, params: dict[str, Any]) -> int:
        spec = MODELS.get(params.get("model", "fal-ai/trellis"))
        return self.max_images if spec and spec["multi"] else 1

    async def generate(self, req: GenerateRequest, on_event: EventFn) -> GenerateResult:
        ok, reason = self.available()
        if not ok:
            raise BackendError(reason)
        if not req.images:
            raise BackendError("fal needs at least one source image.")

        model_id = req.params.get("model", "fal-ai/trellis")
        spec = MODELS.get(model_id)
        if not spec:
            raise BackendError(f"unknown fal model {model_id!r}")
        if len(req.images) > self.image_limit(req.params):
            raise BackendError(f"{spec['label']} accepts one source image; choose a multi-view hosted model to use the full set.")

        uris = [_data_uri(p) for p in req.images]
        body: dict[str, Any] = dict(req.params.get("extra_json") or {})
        body[spec["image_field"]] = uris if spec["multi"] else uris[0]

        headers = {"Authorization": f"Key {settings.fal_key}"}
        started = time.time()

        async with httpx.AsyncClient(timeout=120) as client:
            await on_event("submitting", 0.02, f"POST {QUEUE}/{model_id}")
            r = await client.post(f"{QUEUE}/{model_id}", json=body, headers=headers)
            if r.status_code >= 400:
                raise BackendError(f"fal rejected the job ({r.status_code}): {r.text[:400]}")
            sub = r.json()

            status_url = sub.get("status_url")
            response_url = sub.get("response_url")
            if not status_url or not response_url:
                raise BackendError(f"unexpected fal submit response: {sub}")

            seen: set[str] = set()
            for tick in range(600):  # 600 * 2s = 20 min ceiling
                await asyncio.sleep(2)
                s = (await client.get(status_url, headers=headers, params={"logs": 1})).json()
                state = s.get("status", "")

                for entry in s.get("logs") or []:
                    msg = entry.get("message", "")
                    if msg and msg not in seen:
                        seen.add(msg)
                        await on_event(state.lower(), None, msg)

                if state == "IN_QUEUE":
                    pos = s.get("queue_position")
                    await on_event("queued at fal", 0.05, f"queue position {pos}")
                elif state == "IN_PROGRESS":
                    await on_event("generating", min(0.1 + tick * 0.01, 0.9), None)
                elif state == "COMPLETED":
                    break
                elif state in {"FAILED", "ERROR"}:
                    raise BackendError(f"fal job failed: {s}")
            else:
                raise BackendError("fal job did not finish inside 20 minutes")

            await on_event("downloading", 0.92, "fetching result")
            result = (await client.get(response_url, headers=headers)).json()
            url = _find_glb(result)
            if not url:
                raise BackendError(f"no .glb in fal response: {str(result)[:400]}")

            glb = req.out_dir / f"{req.job_id}.glb"
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(glb, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)

        return GenerateResult(
            glb_path=glb,
            cost_usd=spec["cost_usd"],
            stats={
                "engine": model_id,
                "hosted": True,
                "seconds": round(time.time() - started, 1),
            },
        )
