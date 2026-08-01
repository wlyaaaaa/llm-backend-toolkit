from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backends import BackendRegistry, validate_ollama_options, validate_reasoning_request
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


def _apply_reasoning_request(
    payload: dict[str, Any],
    mapping: dict[str, Any],
    reasoning_mode: str,
) -> None:
    target = payload
    path = mapping["path"]
    for segment in path[:-1]:
        child = target.get(segment)
        if child is None:
            child = {}
            target[segment] = child
        if not isinstance(child, dict):
            raise ValueError(
                f"reasoning_request path conflicts with request field: {segment}"
            )
        target = child
    target[path[-1]] = mapping["off" if reasoning_mode == "off" else "on"]


class OpenAIChatProvider:
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str = "",
        timeout: int = 120,
        cloud: bool = True,
        supports_vision: bool = False,
        thinking_field: str = "",
        reasoning_request: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self.timeout = timeout
        self.cloud = cloud
        self.supports_vision = supports_vision
        if thinking_field and reasoning_request is not None:
            raise ValueError("Configure either thinking_field or reasoning_request, not both")
        if reasoning_request is None and thinking_field:
            reasoning_request = {
                "path": [thinking_field],
                "on": True,
                "off": False,
            }
        self.reasoning_request = (
            validate_reasoning_request(reasoning_request)
            if reasoning_request is not None
            else None
        )

    def invoke(
        self,
        prompt: str,
        native_images: list[str],
        reasoning_mode: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> ProviderResponse:
        del progress_callback
        if not self.api_key:
            raise ProviderCallError(
                ToolError(
                    category="authentication_failed",
                    summary=f"{self.api_key_env or 'Provider API key'} is not configured.",
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
        }
        if self.reasoning_request is not None:
            _apply_reasoning_request(payload, self.reasoning_request, reasoning_mode)
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
            reasoning="",
            tool_calls=list(message.get("tool_calls") or []),
        )

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.model,
            "cloud": self.cloud,
            "configured": bool(self.api_key),
            "live_call_performed": False,
        }


class OllamaProvider:
    cloud = False
    supports_vision = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "qwen-main-v1",
        timeout: int = 900,
        ollama_options: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("LLM_TOOLKIT_OLLAMA_BASE_URL") or "http://127.0.0.1:32100").rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"} and parsed.port == 32101:
            raise ValueError("Internal Ollama backend 32101 is forbidden; use the managed public endpoint.")
        self.timeout = timeout
        self.keep_alive: int | str = os.environ.get("LLM_TOOLKIT_OLLAMA_KEEP_ALIVE", "0")
        self.ollama_options = (
            validate_ollama_options(ollama_options) if ollama_options is not None else {}
        )

    @staticmethod
    def _emit_progress(
        progress_callback: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(event)
        except Exception:
            # Progress display is best-effort observability and must never
            # interrupt or alter the provider result.
            return

    def _invoke_streaming(
        self,
        request: urllib.request.Request,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> ProviderResponse:
        started = time.monotonic()
        public_chunks: list[str] = []
        public_chars = 0
        tool_calls: list[dict[str, Any]] = []
        thinking_chars = 0
        token_events = 0
        final_chunk: dict[str, Any] = {}
        self._emit_progress(
            progress_callback,
            {
                "phase": "connecting",
                "elapsed_seconds": 0.0,
                "content_chars": 0,
                "thinking_active": False,
                "thinking_chars": 0,
                "token_events": 0,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    final_chunk = chunk
                    message = chunk.get("message") or {}
                    thinking_delta = str(message.get("thinking") or "")
                    content_delta = str(message.get("content") or "")
                    if thinking_delta:
                        # Hidden reasoning is counted for activity only and is
                        # intentionally discarded immediately.
                        thinking_chars += len(thinking_delta)
                    if content_delta:
                        public_chunks.append(content_delta)
                        public_chars += len(content_delta)
                    if thinking_delta or content_delta:
                        token_events += 1
                    if message.get("tool_calls"):
                        tool_calls.extend(list(message.get("tool_calls") or []))
                    phase = "generating" if content_delta else "thinking" if thinking_delta else "waiting"
                    event: dict[str, Any] = {
                        "phase": phase,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "content_chars": public_chars,
                        "thinking_active": bool(thinking_delta),
                        "thinking_chars": thinking_chars,
                        "token_events": token_events,
                    }
                    if content_delta:
                        event["content_delta"] = content_delta
                    self._emit_progress(progress_callback, event)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            raise ProviderCallError(classify_provider_error(exc.code, payload)) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            raise ProviderCallError(
                ToolError(
                    category="provider_unavailable",
                    summary=f"Provider transport failed: {type(exc).__name__}",
                    retryable=True,
                    options=("retry-later", "handle-in-codex"),
                )
            ) from exc

        self._emit_progress(
            progress_callback,
            {
                "phase": "completed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "content_chars": public_chars,
                "thinking_active": False,
                "thinking_chars": thinking_chars,
                "token_events": token_events,
            },
        )
        return ProviderResponse(
            content="".join(public_chunks),
            model=str(final_chunk.get("model") or self.model),
            finish_reason=str(final_chunk.get("done_reason") or ""),
            usage={
                "prompt_tokens": final_chunk.get("prompt_eval_count"),
                "completion_tokens": final_chunk.get("eval_count"),
                "prompt_eval_duration_ns": final_chunk.get("prompt_eval_duration"),
                "eval_duration_ns": final_chunk.get("eval_duration"),
                "total_duration_ns": final_chunk.get("total_duration"),
            },
            reasoning="",
            tool_calls=tool_calls,
        )

    def invoke(
        self,
        prompt: str,
        native_images: list[str],
        reasoning_mode: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> ProviderResponse:
        message: dict[str, Any] = {"role": "user", "content": prompt}
        if native_images:
            message["images"] = [
                base64.b64encode(Path(path).read_bytes()).decode("ascii") for path in native_images
            ]
        payload = {
            "model": self.model,
            "messages": [message],
            "stream": progress_callback is not None,
            "think": reasoning_mode != "off",
            "keep_alive": self.keep_alive,
        }
        if self.ollama_options:
            payload["options"] = dict(self.ollama_options)
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        if progress_callback is not None:
            return self._invoke_streaming(request, progress_callback)
        response = _read_json_response(request, self.timeout)
        response_message = response.get("message") or {}
        return ProviderResponse(
            content=str(response_message.get("content") or ""),
            model=str(response.get("model") or self.model),
            finish_reason=str(response.get("done_reason") or ""),
            usage={
                "prompt_tokens": response.get("prompt_eval_count"),
                "completion_tokens": response.get("eval_count"),
                "prompt_eval_duration_ns": response.get("prompt_eval_duration"),
                "eval_duration_ns": response.get("eval_duration"),
                "total_duration_ns": response.get("total_duration"),
            },
            reasoning="",
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


class AgentOnlyProvider:
    def __init__(self, *, model: str, cloud: bool, supports_vision: bool) -> None:
        self.model = model
        self.cloud = cloud
        self.supports_vision = supports_vision

    def invoke(
        self,
        prompt: str,
        native_images: list[str],
        reasoning_mode: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> ProviderResponse:
        del progress_callback
        raise ProviderCallError(
            ToolError(
                category="direct_mode_unavailable",
                summary="This backend is configured for agent execution only.",
                retryable=False,
                options=("use-agent-mode", "handle-in-codex"),
            )
        )

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.model,
            "cloud": self.cloud,
            "configured": True,
            "direct_mode": False,
            "live_call_performed": False,
        }


def provider_from_config(config: dict[str, Any]) -> Any:
    adapter = str(config.get("adapter") or "")
    model = str(config.get("model") or "")
    cloud = bool(config.get("cloud"))
    supports_vision = bool(config.get("supports_vision"))
    if "ollama_options" in config and (adapter != "ollama" or cloud):
        raise ValueError("ollama_options are allowed only for a local ollama backend")
    if adapter == "ollama":
        base_url = os.environ.get(str(config.get("base_url_env") or "")) or str(
            config.get("base_url_default") or "http://127.0.0.1:32100"
        )
        return OllamaProvider(
            base_url=base_url,
            model=model,
            timeout=int(config.get("timeout_seconds") or 900),
            ollama_options=config.get("ollama_options"),
        )
    if adapter == "openai-chat":
        base_url = os.environ.get(str(config.get("base_url_env") or "")) or str(config.get("base_url_default") or "")
        if not base_url:
            raise ValueError("openai-chat backend requires a base URL")
        parsed = urllib.parse.urlparse(base_url)
        if cloud and parsed.scheme.lower() != "https":
            raise ValueError("Cloud openai-chat backend requires an HTTPS base URL")
        return OpenAIChatProvider(
            model=model,
            base_url=base_url,
            api_key_env=str(config.get("api_key_env") or ""),
            timeout=int(config.get("timeout_seconds") or 120),
            cloud=cloud,
            supports_vision=supports_vision,
            thinking_field=str(config.get("thinking_field") or ""),
            reasoning_request=config.get("reasoning_request"),
        )
    if adapter == "agent-only":
        return AgentOnlyProvider(model=model, cloud=cloud, supports_vision=supports_vision)
    raise ValueError(f"Unsupported provider adapter: {adapter}")


def default_providers(registry: BackendRegistry | None = None) -> dict[str, Any]:
    active = registry or BackendRegistry.load()
    return {backend_id: provider_from_config(config) for backend_id, config in active.backends.items()}
