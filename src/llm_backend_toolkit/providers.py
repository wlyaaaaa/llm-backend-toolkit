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
from collections.abc import MutableMapping

from .backends import BackendRegistry, validate_ollama_options, validate_reasoning_request
from .errors import ProviderCallError, ToolError, classify_provider_error
from .transport import normalize_endpoint, open_response


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    model: str | None
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _protocol_error() -> ProviderCallError:
    # Provider payloads can contain prompts, credentials or hidden reasoning.
    # Report the structural failure, never the untrusted response body.
    return ProviderCallError(ToolError(
        category="provider_unavailable", summary="Provider returned an invalid response structure.",
        retryable=False, options=("inspect-provider", "handle-in-codex"),
    ))


def _reported_model(payload: dict[str, Any]) -> str | None:
    value = payload.get("model")
    if value is None:
        return None
    if not isinstance(value, str):
        raise _protocol_error()
    value = value.strip()
    if len(value) > 256 or any(ord(char) < 32 for char in value):
        raise _protocol_error()
    return value or None


def _response_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _protocol_error()
    for name in ("content", "thinking"):
        field = value.get(name)
        if field is not None and not isinstance(field, str):
            raise _protocol_error()
    calls = value.get("tool_calls")
    if calls is not None and (not isinstance(calls, list)
                              or any(not isinstance(call, dict) for call in calls)):
        raise _protocol_error()
    return value


def _read_json_response(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        with open_response(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise _protocol_error()
            return payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        raise ProviderCallError(classify_provider_error(exc.code, payload)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
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
        self.base_url, local_endpoint = normalize_endpoint(base_url)
        if not local_endpoint and not self.base_url.startswith("https://"):
            raise ValueError("Cloud openai-chat backend requires an HTTPS base URL")
        self.api_key_env = api_key_env
        self.api_key = api_key if api_key is not None else os.environ.get(api_key_env, "")
        self.timeout = timeout
        self.cloud = bool(cloud) or not local_endpoint
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
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        request.add_unredirected_header("Authorization", f"Bearer {self.api_key}")
        response = _read_json_response(request, self.timeout)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _protocol_error()
        choice = choices[0]
        message = _response_message(choice.get("message"))
        if response.get("usage") is not None and not isinstance(response["usage"], dict):
            raise _protocol_error()
        return ProviderResponse(
            content=str(message.get("content") or ""),
            model=_reported_model(response),
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
        supports_vision: bool = True,
        managed_base_url: str = "http://127.0.0.1:32100",
    ) -> None:
        self.model = model
        managed, managed_local = normalize_endpoint(managed_base_url)
        self.base_url, local = normalize_endpoint(
            base_url or os.environ.get("LLM_TOOLKIT_OLLAMA_BASE_URL") or managed
        )
        if not managed_local or not local or self.base_url != managed:
            raise ValueError("Local Ollama requires the managed public endpoint registered for LocalGpuBroker; update its owning configuration for a migration")
        if urllib.parse.urlsplit(self.base_url).port in {32101, 11434}:
            raise ValueError("Internal Ollama backend is forbidden; use the managed public endpoint")
        self.cloud = False
        self.supports_vision = bool(supports_vision)
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
        stream_completed = False
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
            with open_response(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if not isinstance(chunk, dict):
                        raise ValueError("Invalid stream object")
                    if chunk.get("error"):
                        raise ProviderCallError(ToolError(
                            category="provider_unavailable",
                            summary="Provider reported a stream error.",
                            retryable=True, options=("retry-later", "handle-in-codex"),
                        ))
                    final_chunk = chunk
                    message = _response_message(chunk.get("message", {}))
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
                    if chunk.get("done") is True:
                        stream_completed = True
                        break
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
            ValueError,
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

        finish_reason = (str(final_chunk.get("done_reason") or "stop")
                         if stream_completed else "stream_incomplete")
        self._emit_progress(
            progress_callback,
            {
                "phase": "completed" if finish_reason in {"stop", "end_turn"} and not tool_calls else "failed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "content_chars": public_chars,
                "thinking_active": False,
                "thinking_chars": thinking_chars,
                "token_events": token_events,
            },
        )
        return ProviderResponse(
            content="".join(public_chunks),
            model=_reported_model(final_chunk),
            finish_reason=finish_reason,
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
        response_message = _response_message(response.get("message"))
        return ProviderResponse(
            content=str(response_message.get("content") or ""),
            model=_reported_model(response),
            finish_reason=(str(response.get("done_reason") or "stop") if response.get("done") is True else "response_incomplete"),
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
            supports_vision=supports_vision,
            managed_base_url=str(config.get("base_url_default") or "http://127.0.0.1:32100"),
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


class LazyProviders(MutableMapping):
    """Keep optional provider failures local to the selected route."""
    def __init__(self, registry: BackendRegistry):
        self.configs = dict(registry.backends)
        self.instances: dict[str, Any] = {}

    def __getitem__(self, key):
        if key not in self.instances:
            self.instances[key] = provider_from_config(self.configs[key])
        return self.instances[key]

    def __setitem__(self, key, value):
        self.instances[key] = value

    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        self.instances.pop(key, None)
        self.configs.pop(key, None)

    def __iter__(self):
        return iter(dict.fromkeys((*self.configs, *self.instances)))

    def __len__(self):
        return len(set(self.configs) | set(self.instances))

    def __contains__(self, key):
        return key in self.configs or key in self.instances


def default_providers(registry: BackendRegistry | None = None) -> LazyProviders:
    return LazyProviders(registry or BackendRegistry.load())
