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

        self.assertEqual(
            ["qwen3.7-plus", "qwen-main-v1"],
            request_schema["properties"]["provider"]["enum"],
        )
        self.assertIn("accepted", response_schema["properties"]["status"]["enum"])
        self.assertEqual("qwen-main-v1", example["provider"])
        self.assertEqual("off", example["reasoning"]["mode"])

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
