from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import MediaError, ToolError


Runner = Callable[..., Any]
EXACT_IMAGE_PURPOSES = {"exact_text", "table", "formula", "scan", "layout", "coordinates"}


@dataclass(frozen=True)
class MediaResult:
    native_images: list[str]
    supplemental_text: list[dict[str, str]]
    artifacts: list[dict[str, str]]
    routes: list[dict[str, str]]


def _default_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, **kwargs)


def _decode_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ValueError("Adapter did not return a JSON object")


def _read_text_artifact(path_value: Any, *, base_dir: Path | None = None) -> tuple[str, str]:
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path.read_text(encoding="utf-8"), str(path)


class MediaProcessor:
    def __init__(
        self,
        *,
        localocr_entry: str | None = None,
        chineseasr_entry: str | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.localocr_entry = localocr_entry or os.environ.get("LLM_TOOLKIT_LOCALOCR_ENTRY")
        self.chineseasr_entry = chineseasr_entry or os.environ.get("LLM_TOOLKIT_CHINESEASR_ENTRY")
        self.runner = runner or _default_runner

    def process(
        self,
        attachments: list[dict[str, Any]],
        *,
        provider_supports_vision: bool,
        mode: str = "auto",
    ) -> MediaResult:
        native_images: list[str] = []
        supplemental_text: list[dict[str, str]] = []
        artifacts: list[dict[str, str]] = []
        routes: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        for attachment in attachments:
            attachment_id = str(attachment.get("id") or "").strip()
            if not attachment_id or attachment_id in seen_ids:
                raise self._error("Attachment IDs must be non-empty and unique")
            seen_ids.add(attachment_id)
            path = Path(str(attachment.get("path") or "")).expanduser().resolve()
            if not path.is_file():
                raise self._error(f"Approved attachment does not exist: {attachment_id}")
            kind = str(attachment.get("kind") or "").lower()
            route = str(attachment.get("route") or mode or "auto").lower()
            purpose = str(attachment.get("purpose") or "").lower()
            if route == "auto":
                if kind == "audio":
                    route = "specialist"
                elif kind == "image" and purpose in EXACT_IMAGE_PURPOSES:
                    route = "specialist"
                elif kind == "image" and provider_supports_vision:
                    route = "native"
                else:
                    route = "specialist"
            if route not in {"native", "specialist"}:
                raise self._error(f"Unsupported media route: {route}")

            if route == "native":
                if kind != "image" or not provider_supports_vision:
                    raise self._error(f"Native route is unavailable for attachment: {attachment_id}")
                native_images.append(str(path))
                routes.append({"id": attachment_id, "kind": kind, "route": route})
                continue

            if kind == "image":
                text, output_path = self._run_localocr(path)
            elif kind == "audio":
                text, output_path = self._run_chineseasr(path)
            else:
                raise self._error(f"Specialist route does not support kind: {kind}")
            supplemental_text.append({"id": attachment_id, "kind": kind, "text": text})
            artifacts.append({"id": attachment_id, "kind": kind, "path": output_path})
            routes.append({"id": attachment_id, "kind": kind, "route": route})

        return MediaResult(native_images, supplemental_text, artifacts, routes)

    def _run_localocr(self, source: Path) -> tuple[str, str]:
        if not self.localocr_entry:
            raise self._error("LocalOCR entry is not configured")
        completed = self.runner(
            [
                "pwsh",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self.localocr_entry,
                str(source),
                "-Engine",
                "auto",
                "-OuterTimeoutSec",
                "120",
                "-TimeoutSec",
                "3600",
                "-StartupTimeoutSec",
                "600",
                "-StopAfter",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3900,
            shell=False,
        )
        if completed.returncode != 0:
            raise self._error(f"LocalOCR adapter exited with code {completed.returncode}")
        try:
            payload = _decode_json(completed.stdout)
            results = payload.get("results") or []
            for result in results:
                output_files = result.get("output_files") or {}
                for key in ("md", "txt"):
                    if output_files.get(key):
                        return _read_text_artifact(
                            output_files[key],
                            base_dir=Path(self.localocr_entry).expanduser().resolve().parent,
                        )
            if payload.get("text"):
                return str(payload["text"]), ""
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise self._error(f"LocalOCR result is unusable: {type(exc).__name__}") from exc
        raise self._error("LocalOCR returned no readable text artifact")

    def _run_chineseasr(self, source: Path) -> tuple[str, str]:
        if not self.chineseasr_entry:
            raise self._error("ChineseASR entry is not configured")
        completed = self.runner(
            [
                "pwsh",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self.chineseasr_entry,
                "-Audio",
                str(source),
                "-Mode",
                "strict",
                "-WaitSec",
                "3600",
                "-StartupTimeoutSec",
                "120",
                "-Json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3900,
            shell=False,
        )
        if completed.returncode != 0:
            raise self._error(f"ChineseASR adapter exited with code {completed.returncode}")
        try:
            payload = _decode_json(completed.stdout)
            final_path = (payload.get("outputs") or {}).get("final")
            if final_path:
                return _read_text_artifact(
                    final_path,
                    base_dir=Path(self.chineseasr_entry).expanduser().resolve().parent.parent,
                )
            if payload.get("text"):
                return str(payload["text"]), ""
            if payload.get("job_id"):
                status = str(payload.get("status") or "pending")
                out_dir = str(payload.get("out_dir") or "")
                detail = f"; inspect {out_dir}" if out_dir else ""
                raise self._error(f"ChineseASR job {payload['job_id']} is {status}{detail}")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise self._error(f"ChineseASR result is unusable: {type(exc).__name__}") from exc
        raise self._error("ChineseASR returned no readable final artifact")

    @staticmethod
    def _error(summary: str) -> MediaError:
        return MediaError(
            ToolError(
                category="tool_failed",
                summary=summary,
                retryable=False,
                options=("inspect-media-adapter", "handle-in-codex"),
            )
        )
