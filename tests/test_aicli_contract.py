"""Executable consumer contract against a real AICLI checkout, with no model call.

Set LLM_TOOLKIT_CONTRACT_AICLI_ROOT to the AICLI source checkout. The local
skill validator requires these tests; standalone builds skip only this external
consumer fixture when the companion is not present.
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from llm_backend_toolkit.agent_runners import AiCliProfileRunner, AgentRunnerError, _bounded_process, _json_values
from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.toolkit import Toolkit


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.environ.get("LLM_TOOLKIT_CONTRACT_AICLI_ROOT") and shutil.which("pwsh"),
                     "AICLI source and PowerShell are required for the external interface contract")
class AicliConsumerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "must-remain-absent"
        source = Path(os.environ["LLM_TOOLKIT_CONTRACT_AICLI_ROOT"])
        manifest = source / "src/AiCliProfileManager/AiCliProfileManager.psd1"
        self.assertTrue(manifest.is_file())
        self.entry = self.root / "aicli-contract-entry.ps1"
        self.entry.write_text(
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n"
            "$OutputEncoding=[Console]::OutputEncoding\n"
            f"Import-Module {quote(manifest)} -Force\n"
            f"exit (Invoke-AiCli -Tokens @($args) -DataRoot {quote(self.data)})\n",
            encoding="utf-8-sig",
        )
        self.registry = BackendRegistry.load()
        backend = self.registry.resolve(None)
        self.route = backend.config["agent_routes"]["codex-cli"]
        self.runner = AiCliProfileRunner(name="codex-cli", engine="codex",
            default_profile=self.route["profile"], entry=str(self.entry))
        self.execution = {"workspace": str(self.root), "policy": "danger-full-access",
            "model": self.route["model"], "profile": self.route["profile"],
            "budget": {"limit_mode": "watchdog_only", "timeout_seconds": 900}}

    def test_generated_structured_arguments_are_accepted_by_actual_parser_without_writes(self):
        result = self.runner.preflight(self.execution)
        self.assertEqual("aicli.machine-run-preflight.v1", result["schema"])
        self.assertEqual(self.route["model"], result["model"])
        self.assertEqual("watchdog_only", result["budgetMode"])
        self.assertFalse(result["modelInvoked"])
        self.assertFalse(result["runtimeCreated"])
        self.assertFalse(self.data.exists())

    def test_actual_parser_still_rejects_legacy_native_flags(self):
        command = self.runner._run_command(self.execution, dry_run=True)
        command += ["--", "exec", "--json", "--disable", "plugins", "-"]
        code, stdout, _, _ = _bounded_process(command, cwd=self.root,
            stdin_text="", timeout_seconds=20, max_output_chars=100000)
        self.assertNotEqual(0, code)
        self.assertIn("error", _json_values(stdout)[-1])
        self.assertFalse(self.data.exists())

    def test_actual_profile_model_mismatch_is_not_reported_as_compatible(self):
        with self.assertRaises(AgentRunnerError) as raised:
            self.runner.preflight({**self.execution, "model": "PUBLIC_WRONG_MODEL"})
        self.assertEqual("agent_model_mismatch", raised.exception.error.category)
        self.assertFalse(self.data.exists())

    def test_toolkit_request_reaches_real_aicli_contract_without_material_reads(self):
        tool = Toolkit(registry=self.registry,
            runners={"codex-cli": self.runner, "data_factory": self.runner})
        request = {"task": {"goal": "Public contract fixture", "sources": [{"path": "does-not-exist.txt"}]},
                   "execution": {"mode": "agent", "runner": "codex-cli", **self.execution}}
        result = tool.preflight(request)
        self.assertEqual("ok", result["status"], result)
        self.assertFalse(result["preflight"]["materials_read"])
        self.assertFalse(result["preflight"]["job_created"])
        self.assertFalse(self.data.exists())

    def test_canonical_skill_agent_example_is_executable(self):
        skill = os.environ.get("LLM_TOOLKIT_CONTRACT_SKILL")
        if not skill:
            self.skipTest("Canonical private skill is not part of the standalone public checkout")
        markdown = (Path(skill) / "references/advanced-usage.md").read_text(encoding="utf-8")
        block = re.search(r"```json\s*(\{.*?\})\s*```", markdown, re.S)
        self.assertIsNotNone(block)
        request = json.loads(block.group(1))
        request["backend"] = self.registry.default_backend
        request["execution"]["runner"] = "codex-cli"
        request["execution"]["workspace"] = str(self.root)
        tool = Toolkit(registry=self.registry,
            runners={"codex-cli": self.runner, "data_factory": self.runner})
        result = tool.preflight(request)
        self.assertEqual("ok", result["status"], result)
        self.assertEqual("watchdog_only", result["preflight"]["aicli"]["budgetMode"])
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()