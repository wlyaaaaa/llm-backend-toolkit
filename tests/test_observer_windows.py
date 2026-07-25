from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER = REPO_ROOT / "scripts" / "Start-LlmBackendObserver.ps1"
INSTALLER = REPO_ROOT / "scripts" / "Install-LlmBackendObserverShortcut.ps1"


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_for_json(command: str) -> dict[str, object]:
    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=f"$result = {command}\n$result | ConvertTo-Json -Compress\n",
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


class ObserverWindowsSourceTests(unittest.TestCase):
    def test_chinese_powershell_scripts_are_utf8_bom(self) -> None:
        for script in (STARTER, INSTALLER):
            with self.subTest(script=script.name):
                self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_launcher_has_standard_edge_paths_and_non_focusing_deduplication(self) -> None:
        source = STARTER.read_text(encoding="utf-8-sig")

        self.assertIn("${env:ProgramFiles(x86)}", source)
        self.assertIn("$env:ProgramFiles", source)
        self.assertIn("$env:LOCALAPPDATA", source)
        self.assertIn("FindProcessIdsByExactTitle", source)
        self.assertIn("EnumWindows", source)
        self.assertIn("StringComparison.Ordinal", source)
        self.assertIn("$process.ProcessName -eq 'msedge'", source)
        self.assertIn('"--app=$($contract.url)"', source)
        self.assertNotIn("AppActivate", source)
        self.assertNotIn("SetForegroundWindow", source)

    def test_installer_uses_known_folder_without_onedrive_assumption(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8-sig")

        self.assertIn("[Environment+SpecialFolder]::DesktopDirectory", source)
        self.assertIn("-WindowStyle Hidden", source)
        self.assertIn("pwsh.exe", source)
        self.assertIn("Microsoft\\Edge\\Application\\msedge.exe", source)
        self.assertIn("$shortcut.IconLocation", source)
        self.assertNotIn("OneDrive", source)


@unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "Windows PowerShell 7 is required")
class ObserverWindowsBehaviorTests(unittest.TestCase):
    def test_launcher_invokes_toolkit_observer_no_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            fake_edge = temp_root / "msedge.exe"
            fake_edge.touch()
            argument_log = temp_root / "arguments.txt"
            fake_toolkit = temp_root / "fake-toolkit.ps1"
            fake_toolkit.write_text(
                f"Set-Content -LiteralPath {_ps_quote(argument_log)} "
                "-Value ($args -join ' ') -Encoding utf8\n"
                "Write-Output '{\"status\":\"ok\","
                "\"url\":\"http://127.0.0.1:8765/\"}'\n",
                encoding="utf-8-sig",
            )
            command = (
                f"& {_ps_quote(STARTER)} "
                f"-ToolkitCommand {_ps_quote(fake_toolkit)} "
                f"-EdgePath {_ps_quote(fake_edge)} "
                f"-WindowTitle {_ps_quote('观察台 CLI 单元测试-不会存在')} "
                "-NoLaunch -PassThru"
            )
            result = _run_for_json(command)

            self.assertEqual("observer --no-open", argument_log.read_text(encoding="utf-8-sig").strip())

        self.assertEqual("planned", result["status"])
        self.assertFalse(result["launched"])

    def test_launcher_test_mode_consumes_observer_json_without_opening_ui(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake_edge = Path(temp) / "msedge.exe"
            fake_edge.touch()
            contract = json.dumps(
                {"status": "ok", "url": "http://127.0.0.1:8765/"},
                ensure_ascii=False,
            )
            command = (
                f"& {_ps_quote(STARTER)} "
                f"-ObserverJson {_ps_quote(contract)} "
                f"-EdgePath {_ps_quote(fake_edge)} "
                f"-WindowTitle {_ps_quote('观察台单元测试-不会存在')} "
                "-NoLaunch -PassThru"
            )
            result = _run_for_json(command)

        self.assertEqual("planned", result["status"])
        self.assertEqual("ok", result["observer_status"])
        self.assertEqual("http://127.0.0.1:8765/", result["url"])
        self.assertFalse(result["launched"])

    def test_launcher_rejects_non_loopback_observer_url(self) -> None:
        completed = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(STARTER),
                "-ObserverJson",
                '{"status":"ok","url":"https://example.com/"}',
                "-NoLaunch",
                "-PassThru",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("loopback", completed.stderr)

    def test_shortcut_test_mode_reports_hidden_pwsh_target_without_creating_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp) / "Redirected Desktop"
            desktop.mkdir()
            command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                "-NoCreate -PassThru"
            )
            result = _run_for_json(command)

            shortcut_path = Path(str(result["shortcut_path"]))
            self.assertEqual(desktop / "模型调用观察台.lnk", shortcut_path)
            self.assertFalse(shortcut_path.exists())

        self.assertEqual("planned", result["status"])
        self.assertTrue(str(result["target_path"]).lower().endswith("pwsh.exe"))
        self.assertTrue(Path(str(result["icon_path"])).is_file())
        self.assertIn("-WindowStyle Hidden", str(result["arguments"]))
        self.assertIn(str(STARTER), str(result["arguments"]))

    def test_shortcut_installer_creates_lnk_only_in_test_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp) / "Redirected Desktop"
            desktop.mkdir()
            command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                "-PassThru"
            )
            result = _run_for_json(command)
            shortcut_path = Path(str(result["shortcut_path"]))

            self.assertEqual("created", result["status"])
            self.assertEqual(desktop / "模型调用观察台.lnk", shortcut_path)
            self.assertTrue(shortcut_path.is_file())


if __name__ == "__main__":
    unittest.main()
