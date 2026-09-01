"""Your own GPU workers on RunPod Serverless.

Same engines as the local adapters, same parameters, running on a card that can
actually build the CUDA extensions. This is the backend a hosted Plumbus sells.

Needs RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID. See deploy/runpod/README.md.
"""
from __future__ import annotations

import asyncio
import base64
import time
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

API = "https://api.runpod.ai/v2"

# Serverless Flex list price, used to attribute a cost to each job. Override with
# PLUMBUS_RUNPOD_USD_PER_HOUR when you move to a different card or a reserved rate.
DEFAULT_USD_PER_HOUR = 1.75


class RunPodBackend(Backend):
    id = "runpod"
    name = "Your GPU workers"
    kind = "hosted"
    blurb = "Your own RunPod Serverless endpoint. CUDA, so PBR texturing actually runs."
    max_images = 1
    accepts_text = False

    def available(self) -> tuple[bool, str]:
        if not settings.runpod_api_key:
            return False, "RUNPOD_API_KEY not set. See deploy/runpod/README.md."
        if not settings.runpod_endpoint_id:
            return False, "RUNPOD_ENDPOINT_ID not set. Deploy the worker first."
        return True, ""

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "engine",
                "label": "Engine",
                "type": "select",
                "default": "trellis",
                "options": [
                    {"value": "trellis", "label": "TRELLIS 2  (best geometry)"},
                    {"value": "hunyuan", "label": "Hunyuan3D 2.1  (best textures)"},
                ],
                "help": "One endpoint serves both. Settings below that do not apply "
                        "to the chosen engine are ignored by the worker.",
            },
            {
                "key": "pipeline_type",
                "label": "TRELLIS resolution",
                "type": "select",
                "default": "1024",
                "options": [
                    {"value": "512", "label": "512  (fast)"},
                    {"value": "1024", "label": "1024  (sharp)"},
                    {"value": "1024_cascade", "label": "1024 cascade  (max detail)"},
                ],
            },
            {
                "key": "texture_size",
                "label": "TRELLIS texture size",
                "type": "select",
                "default": "2048",
                "options": [{"value": v, "label": v} for v in ("512", "1024", "2048")],
            },
            {
                "key": "octree_resolution",
                "label": "Hunyuan octree",
                "type": "select",
                "default": "384",
                "options": [{"value": v, "label": v} for v in ("128", "256", "384", "512")],
            },
            {
                "key": "texture",
                "label": "Hunyuan PBR texturing",
                "type": "bool",
                "default": True,
                "help": "Works here. The CUDA extensions the painter needs are built "
                        "into the worker image.",
            },
            {
                "key": "max_faces",
                "label": "Face budget",
                "type": "int",
                "default": 200000,
                "min": 2000,
                "max": 500000,
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
            raise BackendError("Needs one source image.")

        endpoint = f"{API}/{settings.runpod_endpoint_id}"
        headers = {"Authorization": f"Bearer {settings.runpod_api_key}"}
        params = dict(req.params)
        engine = params.pop("engine", "trellis")

        body = {
            "input": {
                "engine": engine,
                "image": base64.b64encode(req.images[0].read_bytes()).decode(),
                "params": params,
            }
        }

        started = time.time()
        seen: set[str] = set()

        async with httpx.AsyncClient(timeout=180) as client:
            await on_event("submitting", 0.02, f"POST {endpoint}/run  engine={engine}")
            r = await client.post(f"{endpoint}/run", json=body, headers=headers)
            if r.status_code >= 400:
                raise BackendError(f"RunPod rejected the job ({r.status_code}): {r.text[:400]}")

            job_id = r.json().get("id")
            if not job_id:
                raise BackendError(f"no job id in response: {r.text[:300]}")
            await on_event("queued", 0.05, f"runpod job {job_id}")

            result: dict | None = None
            for tick in range(900):  # 900 * 2s = 30 min ceiling
                await asyncio.sleep(2)
                s = (await client.get(f"{endpoint}/status/{job_id}", headers=headers)).json()
                state = (s.get("status") or "").upper()

                for line in (s.get("output") or {}).get("log", []) if isinstance(s.get("output"), dict) else []:
                    if line not in seen:
                        seen.add(line)
                        await on_event("generating", None, line)

                if state == "IN_QUEUE":
                    await on_event("queued", 0.05, None)
                elif state == "IN_PROGRESS":
                    await on_event("generating", min(0.1 + tick * 0.01, 0.9), None)
                elif state == "COMPLETED":
                    result = s.get("output") or {}
                    break
                elif state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                    raise BackendError(f"RunPod job {state}: {str(s)[:500]}")
            else:
                await client.post(f"{endpoint}/cancel/{job_id}", headers=headers)
                raise BackendError("RunPod job did not finish inside 30 minutes, cancelled")

        if not result or result.get("error"):
            detail = (result or {}).get("error", "empty response")
            tb = (result or {}).get("traceback", "")
            raise BackendError(f"worker error: {detail}\n{tb[-600:]}")

        b64 = result.get("glb_b64")
        if not b64:
            raise BackendError(f"worker returned no mesh: {str(result)[:300]}")

        glb = req.out_dir / f"{req.job_id}.glb"
        glb.write_bytes(base64.b64decode(b64))

        for line in result.get("log", []):
            if line not in seen:
                await on_event("generating", None, line)

        wall = time.time() - started
        rate = settings.plumbus_runpod_usd_per_hour or DEFAULT_USD_PER_HOUR
        # Worker seconds, not wall clock: queue time is not billed.
        billed = float(result.get("stats", {}).get("seconds") or wall)

        return GenerateResult(
            glb_path=glb,
            cost_usd=round(billed / 3600 * rate, 4),
            stats={
                **result.get("stats", {}),
                "hosted": "runpod",
                "wall_seconds": round(wall, 1),
            },
        )
