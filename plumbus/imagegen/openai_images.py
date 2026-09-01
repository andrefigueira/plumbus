"""GPT Image 2 via the OpenAI Images API.

Generation goes to /v1/images/generations. Extra views go to /v1/images/edits
with the first view attached, so the model matches the subject it already drew
instead of re-interpreting a description. gpt-image-* always returns b64_json.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .base import (
    EventFn,
    GeneratedImage,
    ImageError,
    ImageProvider,
    ImageRequest,
    ImageResult,
)
from .prompts import MULTIVIEW_ORDER, VIEW_LABELS, build, build_view_edit

API = "https://api.openai.com/v1"

SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536", "2048x2048", "2048x1152"]


class GPTImage2Provider(ImageProvider):
    id = "gpt-image-2"
    name = "GPT Image 2"
    blurb = "OpenAI. Transparent backgrounds, which is exactly what a 3D reference wants."
    max_views = 4

    def available(self) -> tuple[bool, str]:
        if not settings.openai_api_key:
            return False, "OPENAI_API_KEY not set. Add it to .env."
        return True, ""

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "views",
                "label": "Views",
                "type": "views",
                "default": ["three_quarter"],
                "options": [
                    {"value": v, "label": VIEW_LABELS[v]} for v in MULTIVIEW_ORDER
                ],
                "help": "Extra views are drawn from the first one, so identity holds. "
                        "Two to four views make the back of the mesh real instead of guessed.",
            },
            {
                "key": "quality",
                "label": "Quality",
                "type": "select",
                "default": "high",
                "options": [
                    {"value": "low", "label": "low  (cheap draft)"},
                    {"value": "medium", "label": "medium"},
                    {"value": "high", "label": "high  (recommended)"},
                ],
            },
            {
                "key": "size",
                "label": "Size",
                "type": "select",
                "default": "1024x1024",
                "options": [{"value": s, "label": s} for s in SIZES],
                "help": "Square suits a centred single subject. Bigger is not better past 1024 here.",
            },
            {
                "key": "transparent",
                "label": "Transparent background",
                "type": "bool",
                "default": True,
                "help": "Skips background removal downstream entirely, and sidesteps "
                        "TRELLIS's non-commercial RMBG-2.0 stage.",
            },
            {
                "key": "raw_prompt",
                "label": "Send my prompt verbatim",
                "type": "bool",
                "default": False,
                "help": "Off, your text is wrapped in the 3D-reference preset "
                        "(even lighting, no depth of field, nothing cropped). "
                        "On, you get full control and full responsibility.",
            },
        ]

    # ------------------------------------------------------------------ http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.openai_api_key}"}

    @staticmethod
    def _write(payload: dict, dest: Path) -> None:
        try:
            b64 = payload["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError):
            raise ImageError(f"unexpected image response: {str(payload)[:300]}") from None
        dest.write_bytes(base64.b64decode(b64))

    @staticmethod
    def _raise_for(r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        detail = r.text[:400]
        try:
            detail = r.json().get("error", {}).get("message", detail)
        except Exception:  # noqa: BLE001
            pass
        raise ImageError(f"OpenAI returned {r.status_code}: {detail}")

    async def _generate_one(
        self, client: httpx.AsyncClient, prompt: str, p: dict, dest: Path
    ) -> dict:
        body: dict[str, Any] = {
            "model": "gpt-image-2",
            "prompt": prompt,
            "n": 1,
            "quality": p.get("quality", "high"),
            "size": p.get("size", "1024x1024"),
            "output_format": "png",
        }
        if p.get("transparent", True):
            body["background"] = "transparent"

        r = await client.post(f"{API}/images/generations", json=body, headers=self._headers())
        self._raise_for(r)
        data = r.json()
        self._write(data, dest)
        return data.get("usage") or {}

    async def _edit_view(
        self, client: httpx.AsyncClient, prompt: str, p: dict, ref: Path, dest: Path
    ) -> dict:
        form: dict[str, Any] = {
            "model": "gpt-image-2",
            "prompt": prompt,
            "n": "1",
            "quality": p.get("quality", "high"),
            "size": p.get("size", "1024x1024"),
            "output_format": "png",
        }
        if p.get("transparent", True):
            form["background"] = "transparent"

        files = [("image[]", (ref.name, ref.read_bytes(), "image/png"))]
        r = await client.post(
            f"{API}/images/edits", data=form, files=files, headers=self._headers()
        )
        self._raise_for(r)
        data = r.json()
        self._write(data, dest)
        return data.get("usage") or {}

    # --------------------------------------------------------------- public

    async def generate(self, req: ImageRequest, on_event: EventFn) -> ImageResult:
        ok, reason = self.available()
        if not ok:
            raise ImageError(reason)
        if not req.subject.strip():
            raise ImageError("Describe the subject first.")

        p = req.params
        transparent = bool(p.get("transparent", True))
        profile = str(p.get("asset_profile", "object"))
        views = [v for v in req.views if v in VIEW_LABELS] or ["three_quarter"]
        views = views[: self.max_views]
        # The base view anchors the others, so it must come first.
        base_view = views[0]
        rest = views[1:]

        req.out_dir.mkdir(parents=True, exist_ok=True)
        images: list[GeneratedImage] = []
        usage_total: dict[str, int] = {}

        def tally(u: dict) -> None:
            for k, v in (u or {}).items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v

        async with httpx.AsyncClient(timeout=300) as client:
            base_prompt = (
                req.subject
                if req.raw_prompt
                else build(req.subject, base_view, transparent, profile)
            )
            await on_event(f"generating {VIEW_LABELS[base_view].lower()} view")
            base_path = req.out_dir / f"{req.stem}_{base_view}.png"
            tally(await self._generate_one(client, base_prompt, p, base_path))
            images.append(GeneratedImage(base_path, base_view, base_prompt))
            await on_event(f"[view] {VIEW_LABELS[base_view]} ready")

            if rest:
                await on_event(f"deriving {len(rest)} more view(s) from it")

                async def one(view: str) -> GeneratedImage:
                    prompt = build_view_edit(req.subject, view, transparent, profile)
                    dest = req.out_dir / f"{req.stem}_{view}.png"
                    tally(await self._edit_view(client, prompt, p, base_path, dest))
                    # These run concurrently, so report each as it lands.
                    await on_event(f"[view] {VIEW_LABELS[view]} ready")
                    return GeneratedImage(dest, view, prompt)

                results = await asyncio.gather(
                    *(one(v) for v in rest), return_exceptions=True
                )
                for view, res in zip(rest, results):
                    if isinstance(res, Exception):
                        await on_event(f"{VIEW_LABELS[view]} view failed: {res}")
                    else:
                        images.append(res)

        await on_event(f"done, {len(images)} image(s)")
        return ImageResult(images=images, usage=usage_total)
