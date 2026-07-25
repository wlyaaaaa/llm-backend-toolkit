from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .jobs import JobStore
from .observer import ensure_observer, run_observer
from .toolkit import Toolkit


def _read_request(path_value: str) -> dict[str, Any]:
    text = sys.stdin.read() if path_value == "-" else Path(path_value).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Request must be a JSON object")
    return value


def _probe_request(
    backend: str | None,
    case: str,
    attachment: str | None,
    *,
    cloud_allowed: bool = False,
) -> dict[str, Any]:
    if case == "instruction":
        task = {
            "goal": "Return exactly LOCAL_OK and nothing else.",
            "instructions": ["Do not add punctuation or explanation."],
            "inputs": [],
            "expected_output": {"format": "text"},
        }
        media: dict[str, Any] = {}
    elif case == "json":
        task = {
            "goal": "Calculate seven multiplied by eight.",
            "instructions": ["Return strict JSON without a Markdown fence."],
            "inputs": [],
            "expected_output": {"format": "json", "required_keys": ["answer"]},
        }
        media = {}
    elif case == "context":
        task = {
            "goal": "Return strict JSON containing the codename paired with record D.",
            "instructions": ["Use only the supplied records."],
            "inputs": [
                "Record A: amber", "Record B: birch", "Record C: cobalt",
                "Record D: delta-orchid-47", "Record E: ember", "Record F: frost",
            ],
            "expected_output": {"format": "json", "required_keys": ["codename"]},
        }
        media = {}
    elif case == "vision":
        if not attachment:
            raise ValueError("Vision probe requires --attachment")
        task = {
            "goal": "Read the most prominent visible text and return strict JSON.",
            "instructions": ["Return the key text in a field named text."],
            "inputs": [],
            "expected_output": {"format": "json", "required_keys": ["text"]},
        }
        media = {
            "mode": "native",
            "attachments": [{"id": "probe-image", "path": attachment, "kind": "image", "route": "native"}],
        }
    else:
        raise ValueError(f"Unsupported probe case: {case}")
    return {
        **({"backend": backend} if backend else {}),
        "task": task,
        "context": {"mode": "compact", "target_tokens": 2048},
        "reasoning": {"mode": "off"},
        "media": media,
        "privacy": {"cloud_allowed": cloud_allowed},
    }


def _exit_code(result: dict[str, Any]) -> int:
    return 0 if result.get("status") in {"ok", "accepted", "cache_hit"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-backend-toolkit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    invoke = subparsers.add_parser("invoke", help="Invoke one explicitly selected backend")
    invoke.add_argument("--request", default="-", help="JSON request path, or - for stdin")
    submit = subparsers.add_parser("submit", help="Submit a non-blocking background job")
    submit.add_argument("--request", default="-", help="JSON request path, or - for stdin")
    submit.add_argument("--state-dir")
    submit.add_argument("--force", action="store_true", help="Create a new attempt instead of using the request cache")
    job = subparsers.add_parser("job", help="Read a background job without blocking")
    job.add_argument("--id", required=True)
    job.add_argument("--state-dir")
    job.add_argument("--result", action="store_true", help="Include the completed result")
    job.add_argument("--full-result", action="store_true", help="Return full output instead of an artifact preview")
    status = subparsers.add_parser("status", help="Read provider metadata without generation")
    status_target = status.add_mutually_exclusive_group()
    status_target.add_argument("--backend", help="Backend registry ID; omitted means the local default")
    status_target.add_argument("--provider", help="Deprecated backend alias")
    subparsers.add_parser("backends", help="List safe backend registry metadata without generation")
    probe = subparsers.add_parser("probe", help="Run one bounded capability probe")
    probe_target = probe.add_mutually_exclusive_group()
    probe_target.add_argument("--backend", help="Backend registry ID; omitted means the local default")
    probe_target.add_argument("--provider", help="Deprecated backend alias")
    probe.add_argument("--case", required=True, choices=("instruction", "json", "context", "vision"))
    probe.add_argument("--attachment")
    probe.add_argument("--state-dir")
    probe.add_argument("--force", action="store_true", help="Create a new probe attempt instead of using the cache")
    probe.add_argument("--cloud-allowed", action="store_true", help="Explicitly allow this probe to send content to a cloud provider")
    observer = subparsers.add_parser(
        "observer",
        help="Start or reuse the local human-visible model observer",
    )
    observer.add_argument("--state-dir")
    observer.add_argument(
        "--no-open",
        action="store_true",
        help="Return the observer URL without opening a browser window",
    )
    worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--job-id", required=True)
    worker.add_argument("--state-dir", required=True)
    observer_worker = subparsers.add_parser("_observer", help=argparse.SUPPRESS)
    observer_worker.add_argument("--state-dir", required=True)
    observer_worker.add_argument("--runtime-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "invoke":
            toolkit = Toolkit()
            result = toolkit.invoke(_read_request(args.request))
        elif args.command == "submit":
            result = JobStore(args.state_dir).submit(_read_request(args.request), force=args.force)
        elif args.command == "job":
            store = JobStore(args.state_dir)
            if args.result or args.full_result:
                result = store.collect(
                    args.id,
                    full_result=args.full_result,
                )
            else:
                result = store.get(args.id)
        elif args.command == "status":
            toolkit = Toolkit()
            result = toolkit.status(args.backend or args.provider)
        elif args.command == "backends":
            result = Toolkit().catalog()
        elif args.command == "probe":
            backend = args.backend or args.provider
            request = _probe_request(
                backend,
                args.case,
                args.attachment,
                cloud_allowed=args.cloud_allowed,
            )
            result = JobStore(args.state_dir).submit(request, force=args.force)
            result["probe"] = {"backend": backend or "local-default", "case": args.case, "scope": "bounded"}
        elif args.command == "observer":
            result = ensure_observer(
                args.state_dir,
                open_browser=not args.no_open,
            )
        elif args.command == "_worker":
            store = JobStore(args.state_dir)
            progress = None
            try:
                request = store.claim(args.job_id)
                expected = ((request.get("task") or {}).get("expected_output") or {})
                output_format = str(expected.get("format") or "text").lower()
                progress = store.progress_recorder(
                    args.job_id,
                    allow_public_preview=output_format in {"", "text", "plain", "markdown", "md"},
                )
                progress({"phase": "accepted"})
                result = Toolkit().invoke(request, progress_callback=progress)
                store.complete(args.job_id, result)
            except Exception as exc:
                if progress is not None:
                    progress({"phase": "failed"})
                store.fail(args.job_id, f"Worker failed: {type(exc).__name__}")
            return 0
        elif args.command == "_observer":
            run_observer(
                args.state_dir,
                runtime_file=args.runtime_file,
            )
            return 0
        else:
            raise ValueError(f"Unsupported command: {args.command}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "status": "blocked",
            "error": {"category": "invalid_request", "summary": str(exc), "retryable": False},
            "decision": {"owner": "top_model", "options": ["inspect-request"]},
        }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
