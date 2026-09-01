from __future__ import annotations

from .base import (
    GeneratedImage,
    ImageError,
    ImageProvider,
    ImageRequest,
    ImageResult,
)
from .openai_images import GPTImage2Provider
from .prompts import VIEW_LABELS, build

PROVIDERS: dict[str, ImageProvider] = {p.id: p for p in (GPTImage2Provider(),)}


def get(provider_id: str) -> ImageProvider:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise ImageError(f"unknown image provider {provider_id!r}") from None


def describe_all() -> list[dict]:
    out = []
    for p in PROVIDERS.values():
        ok, reason = p.available()
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "blurb": p.blurb,
                "available": ok,
                "reason": reason,
                "max_views": p.max_views,
                "params": p.param_schema(),
            }
        )
    return out


__all__ = [
    "GeneratedImage", "ImageError", "ImageProvider", "ImageRequest", "ImageResult",
    "PROVIDERS", "VIEW_LABELS", "build", "describe_all", "get",
]
