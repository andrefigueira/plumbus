"""Reference-image provider contract.

Deliberately the same shape as backends.Backend: `available()` gates it,
`param_schema()` drives the whole UI form, `generate()` does the work.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

EventFn = Callable[[str], Awaitable[None]]


@dataclass
class ImageRequest:
    subject: str
    out_dir: Path
    stem: str
    views: list[str] = field(default_factory=lambda: ["three_quarter"])
    raw_prompt: bool = False
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedImage:
    path: Path
    view: str
    prompt: str


@dataclass
class ImageResult:
    images: list[GeneratedImage]
    usage: dict[str, Any] = field(default_factory=dict)


class ImageError(RuntimeError):
    pass


class ImageProvider(abc.ABC):
    id: str
    name: str
    blurb: str = ""
    max_views: int = 4

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]: ...

    def param_schema(self) -> list[dict[str, Any]]:
        return []

    @abc.abstractmethod
    async def generate(self, req: ImageRequest, on_event: EventFn) -> ImageResult: ...
