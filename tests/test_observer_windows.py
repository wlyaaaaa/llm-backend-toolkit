from __future__ import annotations

import base64
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER = REPO_ROOT / "scripts" / "Start-LlmBackendObserver.ps1"
INSTALLER = REPO_ROOT / "scripts" / "Install-LlmBackendObserverShortcut.ps1"
ICON_SOURCE = REPO_ROOT / "assets" / "observer-console.svg"
ICON = REPO_ROOT / "assets" / "observer-console.ico"
REQUIRED_ICON_SIZES = {16, 24, 32, 48, 64, 128, 256}


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _pwsh_encoded_command(command: str) -> list[str]:
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def _run_for_json(command: str) -> dict[str, object]:
    wrapped_command = f"$result = {command}\n$result | ConvertTo-Json -Compress\n"
    completed = subprocess.run(
        _pwsh_encoded_command(wrapped_command),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    for line in reversed(completed.stdout.splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"PowerShell did not emit JSON: {completed.stdout!r}")


def _run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _pwsh_encoded_command(command),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _read_shortcut(path: Path) -> dict[str, object]:
    command = (
        "& { "
        "$shell = New-Object -ComObject 'WScript.Shell'; "
        f"$shortcut = $shell.CreateShortcut({_ps_quote(path)}); "
        "try { [pscustomobject] @{ "
        "target_path = $shortcut.TargetPath; "
        "arguments = $shortcut.Arguments; "
        "working_directory = $shortcut.WorkingDirectory; "
        "description = $shortcut.Description; "
        "icon_location = $shortcut.IconLocation "
        "} } finally { "
        "[void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut); "
        "[void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) "
        "} }"
    )
    return _run_for_json(command)


def _write_shortcut(
    path: Path,
    *,
    target_path: str | Path,
    arguments: str,
    working_directory: str | Path,
    description: str,
    icon_location: str,
) -> None:
    command = (
        "& { "
        "$shell = New-Object -ComObject 'WScript.Shell'; "
        f"$shortcut = $shell.CreateShortcut({_ps_quote(path)}); "
        "try { "
        f"$shortcut.TargetPath = {_ps_quote(target_path)}; "
        f"$shortcut.Arguments = {_ps_quote(arguments)}; "
        f"$shortcut.WorkingDirectory = {_ps_quote(working_directory)}; "
        f"$shortcut.Description = {_ps_quote(description)}; "
        f"$shortcut.IconLocation = {_ps_quote(icon_location)}; "
        "$shortcut.Save() "
        "} finally { "
        "[void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut); "
        "[void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) "
        "} }"
    )
    completed = _run_powershell(command)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


def _regular_file_race_hook(
    phase: str,
    path: Path,
    content: bytes,
    *,
    trigger_path: Path | None = None,
) -> str:
    text = content.decode("ascii")
    trigger = trigger_path or path
    return (
        "$testHook = { param($phase, $targetPath, $action) "
        f"if ($phase -ceq {_ps_quote(phase)} -and "
        f"[string]::Equals($targetPath, {_ps_quote(trigger)}, "
        "[StringComparison]::OrdinalIgnoreCase)) { "
        f"[IO.File]::Delete({_ps_quote(path)}); "
        f"[IO.File]::WriteAllBytes({_ps_quote(path)}, "
        f"[Text.Encoding]::ASCII.GetBytes({_ps_quote(text)})) "
        "} }; "
    )


def _foreign_link_race_hook(
    phase: str,
    path: Path,
    foreign_target: Path,
    working_directory: Path,
    *,
    trigger_path: Path | None = None,
) -> str:
    trigger = trigger_path or path
    return (
        "$testHook = { param($phase, $targetPath, $action) "
        f"if ($phase -ceq {_ps_quote(phase)} -and "
        f"[string]::Equals($targetPath, {_ps_quote(trigger)}, "
        "[StringComparison]::OrdinalIgnoreCase)) { "
        f"[IO.File]::Delete({_ps_quote(path)}); "
        "$shell = New-Object -ComObject 'WScript.Shell'; "
        f"$shortcut = $shell.CreateShortcut({_ps_quote(path)}); "
        "try { "
        f"$shortcut.TargetPath = {_ps_quote(foreign_target)}; "
        "$shortcut.Arguments = '-ForeignArgument'; "
        f"$shortcut.WorkingDirectory = {_ps_quote(working_directory)}; "
        "$shortcut.Description = '其他工具'; "
        f"$shortcut.IconLocation = {_ps_quote(f'{foreign_target},0')}; "
        "$shortcut.Save() "
        "} finally { "
        "[void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut); "
        "[void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) "
        "} "
        "} }; "
    )


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
        self.assertIn("[Environment+SpecialFolder]::Programs", source)
        self.assertIn("-WindowStyle Hidden", source)
        self.assertIn("pwsh.exe", source)
        self.assertIn("assets\\observer-console.ico", source)
        self.assertIn("$shortcut.IconLocation", source)
        self.assertNotIn("OneDrive", source)
        self.assertNotIn("Microsoft\\Edge\\Application\\msedge.exe", source)

    def test_project_icon_is_white_and_green_multisize_ico(self) -> None:
        svg = ICON_SOURCE.read_text(encoding="utf-8")
        colors = {color.upper() for color in re.findall(r"#[0-9A-Fa-f]{6}", svg)}
        self.assertEqual({"#FFFFFF", "#16A34A"}, colors)

        icon = ICON.read_bytes()
        reserved, image_type, count = struct.unpack_from("<HHH", icon)
        self.assertEqual(0, reserved)
        self.assertEqual(1, image_type)
        self.assertGreaterEqual(count, len(REQUIRED_ICON_SIZES))

        sizes: set[int] = set()
        for index in range(count):
            offset = 6 + (index * 16)
            width_byte, height_byte, _, _, _, _, byte_count, image_offset = struct.unpack_from(
                "<BBBBHHII", icon, offset
            )
            width = width_byte or 256
            height = height_byte or 256
            self.assertEqual(width, height)
            self.assertLessEqual(image_offset + byte_count, len(icon))
            sizes.add(width)
        self.assertTrue(REQUIRED_ICON_SIZES.issubset(sizes), sizes)


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
            start_menu = Path(temp) / "Redirected Start Menu" / "Programs"
            command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
                "-NoCreate -PassThru"
            )
            result = _run_for_json(command)

            shortcut_path = Path(str(result["shortcut_path"]))
            start_menu_shortcut = Path(str(result["start_menu_shortcut_path"]))
            self.assertEqual(desktop / "模型调用观察台.lnk", shortcut_path)
            self.assertEqual(start_menu / "模型调用观察台.lnk", start_menu_shortcut)
            self.assertFalse(shortcut_path.exists())
            self.assertFalse(start_menu_shortcut.exists())
            self.assertFalse(desktop.exists())
            self.assertFalse(start_menu.exists())

        self.assertEqual("planned", result["status"])
        self.assertTrue(str(result["target_path"]).lower().endswith("pwsh.exe"))
        self.assertEqual(ICON.resolve(), Path(str(result["icon_path"])))
        self.assertIn("-WindowStyle Hidden", str(result["arguments"]))
        self.assertIn(str(STARTER), str(result["arguments"]))

    def test_shortcut_installer_creates_desktop_and_start_menu_links_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp) / "Redirected Desktop"
            start_menu = Path(temp) / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
                "-PassThru"
            )
            first = _run_for_json(command)
            desktop_shortcut = Path(str(first["shortcut_path"]))
            start_menu_shortcut = Path(str(first["start_menu_shortcut_path"]))

            self.assertEqual("created", first["status"])
            self.assertEqual(desktop / "模型调用观察台.lnk", desktop_shortcut)
            self.assertEqual(start_menu / "模型调用观察台.lnk", start_menu_shortcut)
            self.assertTrue(desktop_shortcut.is_file())
            self.assertTrue(start_menu_shortcut.is_file())

            desktop_contract = _read_shortcut(desktop_shortcut)
            start_menu_contract = _read_shortcut(start_menu_shortcut)
            for contract in (desktop_contract, start_menu_contract):
                self.assertEqual(
                    str(first["target_path"]).casefold(),
                    str(contract["target_path"]).casefold(),
                )
                self.assertEqual(first["arguments"], contract["arguments"])
                self.assertEqual(
                    str(first["working_directory"]).casefold(),
                    str(contract["working_directory"]).casefold(),
                )
                self.assertEqual("打开模型调用观察台", contract["description"])
                self.assertEqual(
                    f"{ICON.resolve()},0".casefold(),
                    str(contract["icon_location"]).replace(", 0", ",0").casefold(),
                )

            mtimes = {
                desktop_shortcut: desktop_shortcut.stat().st_mtime_ns,
                start_menu_shortcut: start_menu_shortcut.stat().st_mtime_ns,
            }
            second = _run_for_json(command)
            self.assertEqual("unchanged", second["status"])
            self.assertEqual(
                mtimes,
                {
                    desktop_shortcut: desktop_shortcut.stat().st_mtime_ns,
                    start_menu_shortcut: start_menu_shortcut.stat().st_mtime_ns,
                },
            )

    def test_shortcut_installer_upgrades_owned_legacy_edge_icon_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            desktop = temp_root / "Redirected Desktop"
            start_menu = temp_root / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            legacy_edge = temp_root / "legacy-msedge.exe"
            legacy_edge.touch()
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            installed = _run_for_json(base_command + "-PassThru")
            exact_links = [Path(path) for path in installed["shortcut_paths"]]

            for link in exact_links:
                contract = _read_shortcut(link)
                _write_shortcut(
                    link,
                    target_path=str(contract["target_path"]),
                    arguments=str(contract["arguments"]),
                    working_directory=str(contract["working_directory"]),
                    description=str(contract["description"]),
                    icon_location=f"{legacy_edge},0",
                )

            upgraded = _run_for_json(base_command + "-PassThru")

            self.assertEqual("updated", upgraded["status"])
            for link in exact_links:
                contract = _read_shortcut(link)
                self.assertEqual(
                    f"{ICON.resolve()},0".casefold(),
                    str(contract["icon_location"]).replace(", 0", ",0").casefold(),
                )

    def test_shortcut_whatif_does_not_create_or_remove_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            desktop = temp_root / "Redirected Desktop"
            start_menu = temp_root / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            desktop_shortcut = desktop / "模型调用观察台.lnk"
            start_menu_shortcut = start_menu / "模型调用观察台.lnk"

            install_preview = _run_for_json(base_command + "-WhatIf -PassThru")

            self.assertEqual("skipped", install_preview["status"], install_preview)
            self.assertFalse(desktop_shortcut.exists())
            self.assertFalse(start_menu_shortcut.exists())

            _run_for_json(base_command + "-PassThru")
            before = {
                desktop_shortcut: desktop_shortcut.read_bytes(),
                start_menu_shortcut: start_menu_shortcut.read_bytes(),
            }

            remove_preview = _run_for_json(base_command + "-Remove -WhatIf -PassThru")

            self.assertEqual("skipped", remove_preview["status"])
            self.assertEqual(
                before,
                {
                    desktop_shortcut: desktop_shortcut.read_bytes(),
                    start_menu_shortcut: start_menu_shortcut.read_bytes(),
                },
            )

    def test_shortcut_refuses_foreign_regular_file_without_partial_changes(self) -> None:
        for action in ("install", "remove"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                temp_root = Path(temp)
                desktop = temp_root / "Redirected Desktop"
                start_menu = temp_root / "Redirected Start Menu" / "Programs"
                desktop.mkdir()
                start_menu.mkdir(parents=True)
                legacy_edge = temp_root / "legacy-msedge.exe"
                legacy_edge.touch()
                base_command = (
                    f"& {_ps_quote(INSTALLER)} "
                    f"-DesktopPath {_ps_quote(desktop)} "
                    f"-StartMenuPath {_ps_quote(start_menu)} "
                )
                installed = _run_for_json(base_command + "-PassThru")
                desktop_shortcut = Path(str(installed["shortcut_path"]))
                start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
                desktop_contract = _read_shortcut(desktop_shortcut)
                _write_shortcut(
                    desktop_shortcut,
                    target_path=str(desktop_contract["target_path"]),
                    arguments=str(desktop_contract["arguments"]),
                    working_directory=str(desktop_contract["working_directory"]),
                    description=str(desktop_contract["description"]),
                    icon_location=f"{legacy_edge},0",
                )
                start_menu_shortcut.unlink()
                foreign_content = b"foreign regular file, not a shell link"
                start_menu_shortcut.write_bytes(foreign_content)
                before = {
                    desktop_shortcut: desktop_shortcut.read_bytes(),
                    start_menu_shortcut: start_menu_shortcut.read_bytes(),
                }

                command = base_command + (
                    "-Remove -PassThru" if action == "remove" else "-PassThru"
                )
                completed = _run_powershell(command)

                self.assertNotEqual(0, completed.returncode)
                self.assertIn("拒绝", completed.stderr)
                self.assertEqual(
                    before,
                    {
                        desktop_shortcut: desktop_shortcut.read_bytes(),
                        start_menu_shortcut: start_menu_shortcut.read_bytes(),
                    },
                )

    def test_shortcut_refuses_foreign_link_without_partial_changes(self) -> None:
        for action in ("install", "remove"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                temp_root = Path(temp)
                desktop = temp_root / "Redirected Desktop"
                start_menu = temp_root / "Redirected Start Menu" / "Programs"
                desktop.mkdir()
                start_menu.mkdir(parents=True)
                legacy_edge = temp_root / "legacy-msedge.exe"
                legacy_edge.touch()
                foreign_target = temp_root / "foreign-tool.exe"
                foreign_target.touch()
                base_command = (
                    f"& {_ps_quote(INSTALLER)} "
                    f"-DesktopPath {_ps_quote(desktop)} "
                    f"-StartMenuPath {_ps_quote(start_menu)} "
                )
                installed = _run_for_json(base_command + "-PassThru")
                desktop_shortcut = Path(str(installed["shortcut_path"]))
                start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
                desktop_contract = _read_shortcut(desktop_shortcut)
                start_menu_contract = _read_shortcut(start_menu_shortcut)
                _write_shortcut(
                    desktop_shortcut,
                    target_path=str(desktop_contract["target_path"]),
                    arguments=str(desktop_contract["arguments"]),
                    working_directory=str(desktop_contract["working_directory"]),
                    description=str(desktop_contract["description"]),
                    icon_location=f"{legacy_edge},0",
                )
                _write_shortcut(
                    start_menu_shortcut,
                    target_path=foreign_target,
                    arguments=str(start_menu_contract["arguments"]),
                    working_directory=str(start_menu_contract["working_directory"]),
                    description=str(start_menu_contract["description"]),
                    icon_location=str(start_menu_contract["icon_location"]),
                )
                before = {
                    desktop_shortcut: desktop_shortcut.read_bytes(),
                    start_menu_shortcut: start_menu_shortcut.read_bytes(),
                }

                command = base_command + (
                    "-Remove -PassThru" if action == "remove" else "-PassThru"
                )
                completed = _run_powershell(command)

                self.assertNotEqual(0, completed.returncode)
                self.assertIn("拒绝", completed.stderr)
                self.assertEqual(
                    before,
                    {
                        desktop_shortcut: desktop_shortcut.read_bytes(),
                        start_menu_shortcut: start_menu_shortcut.read_bytes(),
                    },
                )

    def test_shortcut_requires_exact_owned_core_contract(self) -> None:
        mutations = {
            "arguments": lambda value, temp_root: f"{value} -ForeignArgument",
            "working_directory": lambda value, temp_root: str(temp_root),
            "description": lambda value, temp_root: "其他工具",
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                temp_root = Path(temp)
                desktop = temp_root / "Redirected Desktop"
                start_menu = temp_root / "Redirected Start Menu" / "Programs"
                desktop.mkdir()
                start_menu.mkdir(parents=True)
                base_command = (
                    f"& {_ps_quote(INSTALLER)} "
                    f"-DesktopPath {_ps_quote(desktop)} "
                    f"-StartMenuPath {_ps_quote(start_menu)} "
                )
                installed = _run_for_json(base_command + "-PassThru")
                desktop_shortcut = Path(str(installed["shortcut_path"]))
                start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
                contract = _read_shortcut(start_menu_shortcut)
                changed = dict(contract)
                changed[field] = mutate(str(contract[field]), temp_root)
                _write_shortcut(
                    start_menu_shortcut,
                    target_path=str(changed["target_path"]),
                    arguments=str(changed["arguments"]),
                    working_directory=str(changed["working_directory"]),
                    description=str(changed["description"]),
                    icon_location=str(changed["icon_location"]),
                )
                before = {
                    desktop_shortcut: desktop_shortcut.read_bytes(),
                    start_menu_shortcut: start_menu_shortcut.read_bytes(),
                }

                for action in ("install", "remove"):
                    with self.subTest(field=field, action=action):
                        command = base_command + (
                            "-Remove -PassThru" if action == "remove" else "-PassThru"
                        )
                        completed = _run_powershell(command)

                        self.assertNotEqual(0, completed.returncode)
                        self.assertIn("拒绝", completed.stderr)
                        self.assertEqual(
                            before,
                            {
                                desktop_shortcut: desktop_shortcut.read_bytes(),
                                start_menu_shortcut: start_menu_shortcut.read_bytes(),
                            },
                        )

    def test_shortcut_revalidates_both_plans_before_first_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            desktop = temp_root / "Redirected Desktop"
            start_menu = temp_root / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            legacy_edge = temp_root / "legacy-msedge.exe"
            legacy_edge.touch()
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            installed = _run_for_json(base_command + "-PassThru")
            desktop_shortcut = Path(str(installed["shortcut_path"]))
            start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
            desktop_contract = _read_shortcut(desktop_shortcut)
            _write_shortcut(
                desktop_shortcut,
                target_path=str(desktop_contract["target_path"]),
                arguments=str(desktop_contract["arguments"]),
                working_directory=str(desktop_contract["working_directory"]),
                description=str(desktop_contract["description"]),
                icon_location=f"{legacy_edge},0",
            )
            desktop_before = desktop_shortcut.read_bytes()
            foreign_content = b"foreign file introduced after preflight"
            hook = _regular_file_race_hook(
                "after_preflight",
                start_menu_shortcut,
                foreign_content,
            )

            completed = _run_powershell(
                hook + base_command + "-TestHook $testHook -PassThru"
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("冲突", completed.stderr)
            self.assertEqual(desktop_before, desktop_shortcut.read_bytes())
            self.assertEqual(foreign_content, start_menu_shortcut.read_bytes())

    def test_shortcut_revalidates_absent_create_immediately_before_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            desktop = temp_root / "Redirected Desktop"
            start_menu = temp_root / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            desktop_shortcut = desktop / "模型调用观察台.lnk"
            start_menu_shortcut = start_menu / "模型调用观察台.lnk"
            foreign_content = b"foreign create-race file"
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            hook = _regular_file_race_hook(
                "before_target_mutation",
                desktop_shortcut,
                foreign_content,
            )

            completed = _run_powershell(
                hook + base_command + "-TestHook $testHook -PassThru"
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("冲突", completed.stderr)
            self.assertEqual(foreign_content, desktop_shortcut.read_bytes())
            self.assertFalse(start_menu_shortcut.exists())

    def test_shortcut_revalidates_owned_link_immediately_before_update_or_remove(self) -> None:
        for action in ("update", "remove"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                temp_root = Path(temp)
                desktop = temp_root / "Redirected Desktop"
                start_menu = temp_root / "Redirected Start Menu" / "Programs"
                desktop.mkdir()
                start_menu.mkdir(parents=True)
                legacy_edge = temp_root / "legacy-msedge.exe"
                legacy_edge.touch()
                foreign_target = temp_root / "foreign-tool.exe"
                foreign_target.touch()
                base_command = (
                    f"& {_ps_quote(INSTALLER)} "
                    f"-DesktopPath {_ps_quote(desktop)} "
                    f"-StartMenuPath {_ps_quote(start_menu)} "
                )
                installed = _run_for_json(base_command + "-PassThru")
                desktop_shortcut = Path(str(installed["shortcut_path"]))
                start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
                if action == "update":
                    contract = _read_shortcut(desktop_shortcut)
                    _write_shortcut(
                        desktop_shortcut,
                        target_path=str(contract["target_path"]),
                        arguments=str(contract["arguments"]),
                        working_directory=str(contract["working_directory"]),
                        description=str(contract["description"]),
                        icon_location=f"{legacy_edge},0",
                    )
                start_menu_before = start_menu_shortcut.read_bytes()
                hook = _foreign_link_race_hook(
                    "before_target_mutation",
                    desktop_shortcut,
                    foreign_target,
                    temp_root,
                )
                command = hook + base_command + "-TestHook $testHook "
                command += "-Remove -PassThru" if action == "remove" else "-PassThru"

                completed = _run_powershell(command)

                self.assertNotEqual(0, completed.returncode)
                self.assertIn("冲突", completed.stderr)
                self.assertTrue(desktop_shortcut.is_file())
                self.assertEqual(
                    str(foreign_target).casefold(),
                    str(_read_shortcut(desktop_shortcut)["target_path"]).casefold(),
                )
                self.assertEqual(start_menu_before, start_menu_shortcut.read_bytes())

    def test_shortcut_final_revalidates_unchanged_plan_after_other_target_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            desktop = temp_root / "Redirected Desktop"
            start_menu = temp_root / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            legacy_edge = temp_root / "legacy-msedge.exe"
            legacy_edge.touch()
            foreign_target = temp_root / "foreign-tool.exe"
            foreign_target.touch()
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            installed = _run_for_json(base_command + "-PassThru")
            desktop_shortcut = Path(str(installed["shortcut_path"]))
            start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
            desktop_contract = _read_shortcut(desktop_shortcut)
            _write_shortcut(
                desktop_shortcut,
                target_path=str(desktop_contract["target_path"]),
                arguments=str(desktop_contract["arguments"]),
                working_directory=str(desktop_contract["working_directory"]),
                description=str(desktop_contract["description"]),
                icon_location=f"{legacy_edge},0",
            )
            hook = _foreign_link_race_hook(
                "before_target_mutation",
                start_menu_shortcut,
                foreign_target,
                temp_root,
                trigger_path=desktop_shortcut,
            )

            completed = _run_powershell(
                hook + base_command + "-TestHook $testHook -PassThru"
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("冲突", completed.stderr)
            self.assertEqual(
                f"{ICON.resolve()},0".casefold(),
                str(_read_shortcut(desktop_shortcut)["icon_location"])
                .replace(", 0", ",0")
                .casefold(),
            )
            self.assertEqual(
                str(foreign_target).casefold(),
                str(_read_shortcut(start_menu_shortcut)["target_path"]).casefold(),
            )

    def test_shortcut_final_revalidates_absent_plan_after_other_target_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            desktop = temp_root / "Redirected Desktop"
            start_menu = temp_root / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            installed = _run_for_json(base_command + "-PassThru")
            desktop_shortcut = Path(str(installed["shortcut_path"]))
            start_menu_shortcut = Path(str(installed["start_menu_shortcut_path"]))
            start_menu_shortcut.unlink()
            foreign_content = b"foreign file introduced after another removal"
            hook = _regular_file_race_hook(
                "before_target_mutation",
                start_menu_shortcut,
                foreign_content,
                trigger_path=desktop_shortcut,
            )

            completed = _run_powershell(
                hook
                + base_command
                + "-TestHook $testHook -Remove -PassThru"
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("冲突", completed.stderr)
            self.assertFalse(desktop_shortcut.exists())
            self.assertEqual(foreign_content, start_menu_shortcut.read_bytes())

    def test_shortcut_remove_deletes_only_two_exact_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            desktop = Path(temp) / "Redirected Desktop"
            start_menu = Path(temp) / "Redirected Start Menu" / "Programs"
            desktop.mkdir()
            start_menu.mkdir(parents=True)
            base_command = (
                f"& {_ps_quote(INSTALLER)} "
                f"-DesktopPath {_ps_quote(desktop)} "
                f"-StartMenuPath {_ps_quote(start_menu)} "
            )
            installed = _run_for_json(base_command + "-PassThru")
            exact_links = [Path(path) for path in installed["shortcut_paths"]]
            unrelated_links = [
                desktop / "其他工具.lnk",
                start_menu / "其他工具.lnk",
            ]
            for unrelated in unrelated_links:
                unrelated.write_bytes(b"unrelated")

            removed = _run_for_json(base_command + "-Remove -PassThru")

            self.assertEqual("removed", removed["status"])
            self.assertTrue(all(not link.exists() for link in exact_links))
            self.assertTrue(all(link.read_bytes() == b"unrelated" for link in unrelated_links))
            self.assertTrue(desktop.is_dir())
            self.assertTrue(start_menu.is_dir())

            removed_again = _run_for_json(base_command + "-Remove -PassThru")
            self.assertEqual("absent", removed_again["status"])


if __name__ == "__main__":
    unittest.main()
