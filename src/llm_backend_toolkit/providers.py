from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ProviderCallError, ToolError, classify_provider_error


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    model: str
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _read_json_response(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        raise ProviderCallError(classify_provider_error(exc.code, payload)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ProviderCallError(
            ToolError(
                category="provider_unavailable",
                summary=f"Provider transport failed: {type(exc).__name__}",
                retryable=True,
                options=("retry-later", "handle-in-codex"),
            )
        ) from exc


def _image_data_url(path_value: str) -> str:
    path = Path(path_value)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class Qwen37PlusProvider:
    cloud = True
    supports_vision = True

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, timeout: int = 120) -> None:
        self.model = "qwen3.7-plus"
        self.base_url = (base_url or os.environ.get("LLM_TOOLKIT_QWEN_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
        self.timeout = timeout

    def invoke(self, prompt: str, native_images: list[str], reasoning_mode: str) -> ProviderResponse:
        if not self.api_key:
            raise ProviderCallError(
                ToolError(
                    category="authentication_failed",
                    summary="DASHSCOPE_API_KEY is not configured.",
                    retryable=False,
                    options=("repair-credential", "handle-in-codex"),
                )
            )
        content: Any = prompt
        if native_images:
            content = [{"type": "text", "text": prompt}]
            content.extend(
                {"type": "image_url", "image_url": {"url": _image_data_url(path)}}
                for path in native_images
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            "enable_thinking": reasoning_mode != "off",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        response = _read_json_response(request, self.timeout)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ProviderResponse(
            content=str(message.get("content") or ""),
            model=str(response.get("model") or self.model),
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=dict(response.get("usage") or {}),
            reasoning=str(message.get("reasoning_content") or ""),
            tool_calls=list(message.get("tool_calls") or []),
        )

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.model,
            "cloud": True,
            "configured": bool(self.api_key),
            "live_call_performed": False,
        }


class OllamaProvider:
    cloud = False
    supports_vision = True

    def __init__(self, *, base_url: str | None = None, model: str = "qwen-main-v1", timeout: int = 900) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("LLM_TOOLKIT_OLLAMA_BASE_URL") or "http://127.0.0.1:32100").rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"} and parsed.port == 32101:
            raise ValueError("Internal Ollama backend 32101 is forbidden; use the managed public endpoint.")
        self.timeout = timeout
        self.keep_alive: int | str = os.environ.get("LLM_TOOLKIT_OLLAMA_KEEP_ALIVE", "0")

    def invoke(self, prompt: str, native_images: list[str], reasoning_mode: str) -> ProviderResponse:
        message: dict[str, Any] = {"role": "user", "content": prompt}
        if native_images:
            message["images"] = [
                base64.b64encode(Path(path).read_bytes()).decode("ascii") for path in native_images
            ]
        payload = {
            "model": self.model,
            "messages": [message],
            "stream": False,
            "think": reasoning_mode != "off",
            "keep_alive": self.keep_alive,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        response = _read_json_response(request, self.timeout)
        response_message = response.get("message") or {}
        return ProviderResponse(
            content=str(response_message.get("content") or ""),
            model=str(response.get("model") or self.model),
            finish_reason=str(response.get("done_reason") or ""),
            usage={
                "prompt_tokens": response.get("prompt_eval_count"),
                "completion_tokens": response.get("eval_count"),
                "total_duration_ns": response.get("total_duration"),
            },
            reasoning=str(response_message.get("thinking") or ""),
            tool_calls=list(response_message.get("tool_calls") or []),
        )

    def status(self) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}/_gpu_broker/status", method="GET")
        broker = _read_json_response(request, 10)
        show_request = urllib.request.Request(
            f"{self.base_url}/api/show",
            data=json.dumps({"model": self.model, "verbose": False}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        show = _read_json_response(show_request, 15)
        tags_request = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
        tags = _read_json_response(tags_request, 10)
        version_request = urllib.request.Request(f"{self.base_url}/api/version", method="GET")
        version = _read_json_response(version_request, 10)
        details = show.get("details") or {}
        aliases = {self.model, f"{self.model}:latest"}
        aliases.add(self.model.removesuffix(":latest"))
        tag = next(
            (
                item
                for item in tags.get("models") or []
                if str(item.get("name") or item.get("model") or "") in aliases
                or str(item.get("name") or item.get("model") or "").removesuffix(":latest")
                == self.model.removesuffix(":latest")
            ),
            {},
        )
        model_info = show.get("model_info") or {}
        context_length = next(
            (value for key, value in model_info.items() if str(key).endswith(".context_length")),
            None,
        )
        return {
            "provider": self.model,
            "cloud": False,
            "broker": {
                "ok": bool(broker.get("ok")),
                "lease": broker.get("lease"),
                "active_ollama_requests": broker.get("active_ollama_requests"),
            },
            "model": {
                "parent_model": details.get("parent_model"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "digest": tag.get("digest"),
                "modified_at": tag.get("modified_at"),
                "context_length": context_length,
                "capabilities": list(show.get("capabilities") or []),
            },
            "runtime": {"ollama_version": version.get("version")},
            "live_call_performed": False,
        }


def default_providers() -> dict[str, Any]:
    return {
        "qwen3.7-plus": Qwen37PlusProvider(),
        "qwen-main-v1": OllamaProvider(),
    }
