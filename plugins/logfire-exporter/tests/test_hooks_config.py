from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = ROOT / "hooks" / "hooks.json"
WINDOWS_LAUNCHER_PATH = ROOT / "scripts" / "run_codex_logfire_hook.cmd"


class HooksConfigTests(unittest.TestCase):
    def command_hooks(self) -> Iterator[tuple[str, dict[str, Any]]]:
        config = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        for event_name, groups in config["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    if hook["type"] == "command":
                        yield event_name, hook

    def test_all_commands_have_platform_launchers_and_expected_timeouts(self) -> None:
        expected_timeouts = {
            "UserPromptSubmit": 10,
            "Stop": 30,
        }
        hooks = list(self.command_hooks())

        self.assertEqual({event_name for event_name, _ in hooks}, set(expected_timeouts))
        for event_name, hook in hooks:
            self.assertIn("${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}", hook["command"])
            windows_command = hook["commandWindows"]
            self.assertIn("PLUGIN_ROOT", windows_command)
            self.assertIn("run_codex_logfire_hook.cmd", windows_command)
            self.assertNotIn(" sh ", f" {windows_command.lower()} ")
            self.assertEqual(hook["timeout"], expected_timeouts[event_name])

    @unittest.skipUnless(sys.platform == "win32", "requires Windows command shells")
    def test_windows_hooks_receive_input_in_powershell_and_cmd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="logfire plugin ") as directory:
            plugin_root = Path(directory)
            scripts = plugin_root / "scripts"
            scripts.mkdir()
            for name in ("run_codex_logfire_hook.cmd", "codex_logfire_hook.py"):
                shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            config = plugin_root / "config.env"
            config.write_text("", encoding="utf-8")
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("LOGFIRE_", "CODEX_LOGFIRE_", "OTEL_EXPORTER_OTLP"))
            }
            env.update({
                "PLUGIN_ROOT": str(plugin_root),
                "CODEX_LOGFIRE_CONFIG_FILE": str(config),
                "CODEX_LOGFIRE_PYTHON": sys.executable,
                "CODEX_LOGFIRE_DEBUG": "true",
                "PYTHONIOENCODING": "cp949:surrogateescape",
                "PYTHONUTF8": "0",
            })
            for shell in ("powershell.exe", "cmd.exe"):
                state = plugin_root / shell
                env["CODEX_LOGFIRE_STATE_DIR"] = str(state)
                for event, hook in self.command_hooks():
                    with self.subTest(shell=shell, event=event):
                        command = hook["commandWindows"].replace("${PLUGIN_ROOT}", str(plugin_root))
                        if shell == "powershell.exe":
                            args = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
                        else:
                            args = f'cmd.exe /d /s /c "{command}"'
                        result = subprocess.run(
                            args,
                            input=json.dumps({
                                "hook_event_name": event,
                                "session_id": "windows-shell-test",
                                "turn_id": "turn-1",
                                "prompt": "테스트 안녕 🙂",
                                "last_assistant_message": "한글 응답 👋",
                            }, ensure_ascii=False).encode("utf-8"),
                            capture_output=True,
                            env=env,
                            timeout=10,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        log = state / "logs" / "logfire-exporter.log"
                        self.assertTrue(log.is_file(), "the Python hook did not run")
                        diagnostics = log.read_text(encoding="utf-8")
                        self.assertIn(f"dispatch event={event!r}", diagnostics)
                        self.assertNotIn("hook failed open", diagnostics)
                        if event == "UserPromptSubmit":
                            turns = list((state / "turns").glob("*.json"))
                            self.assertEqual(len(turns), 1)
                            self.assertEqual(json.loads(turns[0].read_text(encoding="utf-8"))["prompt"], "테스트 안녕 🙂")
                self.assertFalse(list((state / "turns").glob("*.json")))

    def test_windows_launcher_runs_real_hook_and_fails_open(self) -> None:
        launcher = WINDOWS_LAUNCHER_PATH.read_text(encoding="utf-8")

        self.assertIn("codex_logfire_hook.py", launcher)
        self.assertIn("CODEX_LOGFIRE_PYTHON", launcher)
        self.assertIn("exit /b 0", launcher.lower())

    def test_windows_launcher_falls_back_after_failed_candidates(self) -> None:
        launcher = WINDOWS_LAUNCHER_PATH.read_text(encoding="utf-8")

        self.assertIn(
            'python3 "%~dp0codex_logfire_hook.py"\n    if not errorlevel 1 exit /b 0',
            launcher,
        )
        self.assertIn(
            'py -3 "%~dp0codex_logfire_hook.py"\n    if not errorlevel 1 exit /b 0',
            launcher,
        )


if __name__ == "__main__":
    unittest.main()
