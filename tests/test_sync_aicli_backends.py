import copy
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "sync_aicli_backends.py"
SPEC = importlib.util.spec_from_file_location("sync_aicli_backends", SCRIPT_PATH)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(SYNC)


MAIN_IDS = (
    "codex-ollama-main",
    "claude-ollama-main",
    "qwen-code-ollama-main",
    "opencode-ollama-main",
)


def registry() -> dict:
    return json.loads(
        (ROOT / "src" / "llm_backend_toolkit" / "default_backends.json").read_text(
            encoding="utf-8"
        )
    )


def write_profile(data: Path, profile_id: str, model: str, *, display: str, images: bool, output: int, catalog: bool) -> dict:
    engine, transport = SYNC.PROFILE_CONTRACTS[profile_id]
    raw = {
        "id": profile_id,
        "displayName": display,
        "engine": engine,
        "provider": "ollama",
        "transport": transport,
        "endpoint": "http://127.0.0.1:32100/v1",
        "models": {"primary": model, "small": model},
        "modelMetadata": {model: {"contextWindowTokens": 262144, "outputWindowTokens": output}},
        "compatibility": {"ollamaArtifact": {"numCtx": 262144}},
        "capabilities": {"tools": True, "streaming": True, "images": images, "machineRun": True},
        "auth": {"type": "none"},
    }
    if catalog:
        catalog_name = f"{profile_id}.json"
        raw["codexModelCatalog"] = catalog_name
        (data / "model-catalogs" / catalog_name).write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": model,
                            "context_window": 262144,
                            "max_context_window": 262144,
                            "input_modalities": ["text", "image"] if images else ["text"],
                            "default_reasoning_level": "max",
                            "supported_reasoning_levels": [{"effort": "max"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    (data / "providers" / f"{profile_id}.json").write_text(json.dumps(raw), encoding="utf-8")
    return raw


def write_aicli(root: Path, *, main_model: str, review_model: str) -> Path:
    data = root / "data"
    (data / "providers").mkdir(parents=True)
    (data / "model-catalogs").mkdir()
    for profile_id in MAIN_IDS:
        write_profile(
            data,
            profile_id,
            main_model,
            display="Engine + Qwen3.8 27B",
            images=True,
            output=32768,
            catalog=profile_id == "codex-ollama-main",
        )
    write_profile(
        data,
        "codex-ollama-review",
        review_model,
        display="Codex CLI + Qwen3.6 35B",
        images=False,
        output=8192,
        catalog=True,
    )
    write_profile(
        data,
        "codex-ollama-qwen3-6-35b-abliterated",
        "aicli-qwen3.6-35b-abliterated-256k:2026-09-15",
        display="Codex CLI + Qwen3.6 35B Abliterated",
        images=False,
        output=32768,
        catalog=True,
    )
    write_profile(
        data,
        "codex-ollama-qwen3-8-27b-abliterated",
        "aicli-qwen3.8-27b-abliterated-256k:2026-09-15",
        display="Codex CLI + Qwen3.8 27B 去限制版（256K）",
        images=True,
        output=32768,
        catalog=True,
    )
    return data


def fingerprints(data: Path, *, codex: str = "codex-fingerprint", review: str = "review-fingerprint") -> dict[str, str]:
    values = {profile_id: f"{profile_id}-fingerprint" for profile_id in MAIN_IDS}
    values["codex-ollama-main"] = codex
    values["codex-ollama-review"] = review
    values["codex-ollama-qwen3-6-35b-abliterated"] = "abliterated-fingerprint"
    values["codex-ollama-qwen3-8-27b-abliterated"] = "abliterated27-fingerprint"
    return values


class SyncAicliBackendsTests(unittest.TestCase):
    def test_current_equivalent_inputs_are_byte_noop(self):
        source = registry()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "aicli"
            data = write_aicli(
                root,
                main_model=source["backends"]["local-default"]["model"],
                review_model=source["backends"]["local-crosscheck-35b"]["model"],
            )
            source["backends"]["local-default"]["agent_routes"]["data_factory"]["evidence"]["profile_fingerprint"] = "codex-fingerprint"
            source["backends"]["local-default"]["agent_routes"]["codex-cli"]["evidence"]["profile_fingerprint"] = "codex-fingerprint"
            source["backends"]["local-crosscheck-35b"]["agent_routes"]["codex-cli"]["evidence"]["profile_fingerprint"] = "review-fingerprint"
            source["backends"]["local-qwen3-6-35b-abliterated"]["agent_routes"]["codex-cli"]["evidence"]["profile_fingerprint"] = "abliterated-fingerprint"
            source["backends"]["local-qwen3-8-27b-abliterated"]["agent_routes"]["codex-cli"]["evidence"]["profile_fingerprint"] = "abliterated27-fingerprint"
            candidate, changes = SYNC.build_plan(source, data, profile_fingerprints=fingerprints(data))
        self.assertEqual([], changes)
        self.assertEqual(source, candidate)

    def test_new_main_and_review_models_clear_only_their_old_evidence(self):
        source = registry()
        with tempfile.TemporaryDirectory() as temp:
            data = write_aicli(Path(temp) / "aicli", main_model="next-main", review_model="next-review")
            candidate, changes = SYNC.build_plan(
                source,
                data,
                main_direct_vision=True,
                review_direct_vision=False,
                profile_fingerprints=fingerprints(data),
            )
        self.assertTrue(changes)
        default_change = next(change for change in changes if change.get("backend") == "local-default" and "route" not in change)
        self.assertIn(
            {
                "field": "model",
                "before": source["backends"]["local-default"]["model"],
                "after": "next-main",
            },
            default_change["fields"],
        )
        default = candidate["backends"]["local-default"]
        self.assertEqual("next-main", default["model"])
        self.assertTrue(default["supports_vision"])
        for name in ("data_factory", "codex-cli", "claude-code", "qwen-code", "opencode"):
            self.assertEqual("next-main", default["agent_routes"][name]["model"])
        evidence = default["agent_routes"]["codex-cli"]["evidence"]
        self.assertFalse(evidence["live_verified"])
        self.assertEqual("pending_reacceptance", evidence["capability_acceptance_state"])
        self.assertNotIn("historical_model", evidence)
        review = candidate["backends"]["local-crosscheck-35b"]
        self.assertEqual("next-review", review["model"])
        self.assertFalse(review["supports_vision"])
        self.assertEqual(32768, review["ollama_options"]["num_predict"])

    def test_main_profile_disagreement_blocks_before_any_registry_change(self):
        source = registry()
        before = copy.deepcopy(source)
        with tempfile.TemporaryDirectory() as temp:
            data = write_aicli(Path(temp) / "aicli", main_model="consistent", review_model="review")
            raw = json.loads((data / "providers" / "opencode-ollama-main.json").read_text(encoding="utf-8"))
            raw["models"]["primary"] = "different"
            raw["models"]["small"] = "different"
            raw["modelMetadata"]["different"] = raw["modelMetadata"].pop("consistent")
            (data / "providers" / "opencode-ollama-main.json").write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(SYNC.SyncError, "main models"):
                SYNC.build_plan(source, data, main_direct_vision=True, review_direct_vision=False, profile_fingerprints=fingerprints(data))
        self.assertEqual(before, source)

    def test_context_and_main_output_guards_fail_before_writing(self):
        source = registry()
        with tempfile.TemporaryDirectory() as temp:
            data = write_aicli(Path(temp) / "aicli", main_model="next", review_model="review")
            raw_path = data / "providers" / "claude-ollama-main.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["modelMetadata"]["next"]["contextWindowTokens"] = 131072
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(SYNC.SyncError, "context"):
                SYNC.build_plan(source, data, main_direct_vision=True, review_direct_vision=False, profile_fingerprints=fingerprints(data))
            source["backends"]["local-default"]["ollama_options"]["num_predict"] = 40000
            raw["modelMetadata"]["next"]["contextWindowTokens"] = 262144
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(SYNC.SyncError, "num_predict"):
                SYNC.build_plan(source, data, main_direct_vision=True, review_direct_vision=False, profile_fingerprints=fingerprints(data))

    def test_profile_or_catalog_raw_drift_invalidates_same_model_evidence(self):
        source = registry()
        main_model = source["backends"]["local-default"]["model"]
        review_model = source["backends"]["local-crosscheck-35b"]["model"]
        with tempfile.TemporaryDirectory() as temp:
            data = write_aicli(Path(temp) / "aicli", main_model=main_model, review_model=review_model)
            baseline = fingerprints(data)["codex-ollama-main"]
            for name in ("data_factory", "codex-cli"):
                source["backends"]["local-default"]["agent_routes"][name]["evidence"]["profile_fingerprint"] = baseline
            raw_path = data / "providers" / "claude-ollama-main.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["notes"] = "changed profile contract"
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            changed_fingerprints = fingerprints(data, codex="changed-codex-fingerprint")
            candidate, changes = SYNC.build_plan(source, data, profile_fingerprints=changed_fingerprints)
        self.assertTrue(changes)
        evidence = candidate["backends"]["local-default"]["agent_routes"]["codex-cli"]["evidence"]
        self.assertFalse(evidence["live_verified"])
        self.assertEqual("pending_reacceptance", evidence["capability_acceptance_state"])

    def test_same_model_direct_capability_override_is_explicit_and_invalidates_evidence(self):
        source = registry()
        main_model = source["backends"]["local-default"]["model"]
        review_model = source["backends"]["local-crosscheck-35b"]["model"]
        with tempfile.TemporaryDirectory() as temp:
            data = write_aicli(Path(temp) / "aicli", main_model=main_model, review_model=review_model)
            candidate, changes = SYNC.build_plan(source, data, main_direct_vision=False, profile_fingerprints=fingerprints(data))
        self.assertTrue(changes)
        self.assertFalse(candidate["backends"]["local-default"]["supports_vision"])
        self.assertFalse(candidate["backends"]["local-default"]["agent_routes"]["codex-cli"]["evidence"]["live_verified"])

    def test_review_cannot_equal_main_model(self):
        source = registry()
        main_model = source["backends"]["local-default"]["model"]
        with tempfile.TemporaryDirectory() as temp:
            data = write_aicli(Path(temp) / "aicli", main_model=main_model, review_model=main_model)
            with self.assertRaisesRegex(SYNC.SyncError, "review model"):
                SYNC.build_plan(source, data, profile_fingerprints=fingerprints(data))

    def test_apply_error_never_partially_rewrites_registry(self):
        source = registry()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "aicli"
            data = write_aicli(
                root,
                main_model=source["backends"]["local-default"]["model"],
                review_model=source["backends"]["local-crosscheck-35b"]["model"],
            )
            raw_path = data / "providers" / "qwen-code-ollama-main.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw.pop("capabilities")
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            registry_path = Path(temp) / "registry.json"
            registry_path.write_text(json.dumps(source, indent=2), encoding="utf-8")
            before = registry_path.read_bytes()
            with patch.object(SYNC, "_profile_fingerprints", return_value=fingerprints(data)), contextlib.redirect_stderr(io.StringIO()):
                code = SYNC.main(["--aicli-root", str(root), "--registry", str(registry_path), "--apply"])
            self.assertEqual(2, code)
            self.assertEqual(before, registry_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
