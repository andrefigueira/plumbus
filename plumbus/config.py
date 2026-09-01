from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_prefix="", extra="ignore"
    )

    fal_key: str = ""
    openai_api_key: str = ""
    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    plumbus_runpod_usd_per_hour: float = 0.0

    plumbus_output_dir: Path = ROOT / "outputs"
    plumbus_upload_dir: Path = ROOT / "uploads"
    plumbus_db_path: Path = ROOT / "plumbus.db"

    plumbus_trellis_path: Path = ROOT / "vendor" / "trellis-mac"
    plumbus_hunyuan_path: Path = ROOT / "vendor" / "Hunyuan3D-2.1-mac"
    plumbus_trellis_python: Path = ROOT / "vendor" / "trellis-mac" / ".venv" / "bin" / "python"
    plumbus_hunyuan_python: Path = ROOT / "vendor" / "Hunyuan3D-2.1-mac" / ".venv" / "bin" / "python"

    plumbus_default_backend: str = "trellis"
    plumbus_host: str = "127.0.0.1"
    plumbus_port: int = 8420

    @field_validator("plumbus_runpod_usd_per_hour", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        """An empty value in .env means "not configured", not a parse error."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0.0
        return v

    @field_validator(
        "plumbus_output_dir", "plumbus_upload_dir", "plumbus_db_path",
        "plumbus_trellis_path", "plumbus_hunyuan_path",
        "plumbus_trellis_python", "plumbus_hunyuan_python",
        mode="after",
    )
    @classmethod
    def _anchor_to_project(cls, v: Path) -> Path:
        """A relative path in .env means "relative to the project", not to $PWD.

        Use abspath, not Path.resolve(). uv venv pythons are symlinks into the
        uv store; resolve() walks through them and we lose the venv's site-packages.
        """
        p = v if v.is_absolute() else (ROOT / v)
        return Path(os.path.abspath(p))

    @property
    def output_dir(self) -> Path:
        self.plumbus_output_dir.mkdir(parents=True, exist_ok=True)
        return self.plumbus_output_dir

    @property
    def upload_dir(self) -> Path:
        self.plumbus_upload_dir.mkdir(parents=True, exist_ok=True)
        return self.plumbus_upload_dir


settings = Settings()
