from __future__ import annotations

from .base import (
    Backend,
    BackendError,
    EventFn,
    GenerateRequest,
    GenerateResult,
)
from .fal import FalBackend
from .hunyuan import HunyuanBackend
from .runpod_remote import RunPodBackend
from .trellis import TrellisBackend

REGISTRY: dict[str, Backend] = {
    b.id: b
    for b in (TrellisBackend(), HunyuanBackend(), RunPodBackend(), FalBackend())
}


def get(backend_id: str) -> Backend:
    try:
        return REGISTRY[backend_id]
    except KeyError:
        raise BackendError(f"unknown backend {backend_id!r}") from None


def describe_all() -> list[dict]:
    out = []
    for b in REGISTRY.values():
        ok, reason = b.available()
        out.append(
            {
                "id": b.id,
                "name": b.name,
                "kind": b.kind,
                "blurb": b.blurb,
                "available": ok,
                "reason": reason,
                "max_images": b.max_images,
                "accepts_text": b.accepts_text,
                "params": b.param_schema(),
            }
        )
    return out


__all__ = [
    "Backend",
    "BackendError",
    "EventFn",
    "GenerateRequest",
    "GenerateResult",
    "REGISTRY",
    "get",
    "describe_all",
]
