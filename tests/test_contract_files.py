import json
import re
import tomllib
import unittest
from pathlib import Path

from llm_backend_toolkit import __version__


ROOT = Path(__file__).resolve().parents[1]


class ContractFileTests(unittest.TestCase):
    def test_package_versions_match(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(project["project"]["version"], __version__)

    def test_schemas_and_example_are_valid_json(self):
        request_schema = json.loads((ROOT / "schemas" / "request.schema.json").read_text(encoding="utf-8"))
        response_schema = json.loads((ROOT / "schemas" / "response.schema.json").read_text(encoding="utf-8"))
        example = json.loads((ROOT / "examples" / "local-request.json").read_text(encoding="utf-8"))
        cloud_agent = json.loads((ROOT / "examples" / "cloud-agent-request.json").read_text(encoding="utf-8"))
        fast_middle = json.loads(
            (ROOT / "examples" / "fast-middle-agent-request.json").read_text(encoding="utf-8")
        )

        self.assertEqual("string", request_schema["properties"]["backend"]["type"])
        self.assertNotIn("enum", request_schema["properties"]["backend"])
        self.assertNotIn("provider", request_schema["required"])
        execution = request_schema["properties"]["execution"]
        self.assertEqual(["direct", "agent"], execution["properties"]["mode"]["enum"])
        self.assertNotIn("enum", execution["properties"]["runner"])
        self.assertIn("pattern", execution["properties"]["runner"])
        cache_key = execution["properties"]["cache_key"]
        self.assertEqual(512, cache_key["maxLength"])
        self.assertIn("pattern", cache_key)
        self.assertIn("execution_receipt", response_schema["properties"])
        self.assertIn("cache_identity", response_schema["properties"])
        self.assertIn("accepted", response_schema["properties"]["status"]["enum"])
        self.assertEqual("local-default", example["backend"])
        self.assertEqual("off", example["reasoning"]["mode"])
        self.assertEqual("cloud-qwen-plus", cloud_agent["backend"])
        self.assertTrue(cloud_agent["privacy"]["cloud_allowed"])
        self.assertEqual("agent", cloud_agent["execution"]["mode"])
        self.assertNotIn("runner", cloud_agent["execution"])
        self.assertEqual("fast-middle-agent", fast_middle["backend"])
        self.assertTrue(fast_middle["privacy"]["cloud_allowed"])
        self.assertEqual("read-only", fast_middle["execution"]["policy"])
        self.assertEqual("data_factory", fast_middle["execution"]["runner"])

    def test_public_files_do_not_contain_secret_like_values(self):
        forbidden = (
            re.compile(r"(?i)authorization\s*:\s*bearer\s+ghp_[A-Za-z0-9]{20,}"),
            re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
            re.compile(r'"apiKey"\s*:\s*"[^"\n]{8,}"'),
        )
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
                continue
            if path.suffix.lower() not in {".py", ".md", ".json", ".toml", ".txt"} and path.name not in {"LICENSE"}:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(pattern.search(text), f"secret-like token in {path}")


if __name__ == "__main__":
    unittest.main()
