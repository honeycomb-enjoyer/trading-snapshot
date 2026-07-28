"""Brokerless unit tests for the P0-T13 deployment dry-run guard."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "deployment_preflight", ROOT / "scripts" / "deployment_preflight.py"
)
deployment_preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deployment_preflight
assert SPEC.loader is not None
SPEC.loader.exec_module(deployment_preflight)


def completed(command: list[str], *, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


class DeploymentPreflightTests(unittest.TestCase):
    def test_dirty_worktree_is_blocked_before_any_other_check(self):
        with patch.object(
            deployment_preflight,
            "_git",
            return_value=completed(["git"], stdout=" M accounts.py\n"),
        ):
            with self.assertRaisesRegex(deployment_preflight.PreflightError, "worktree is dirty"):
                deployment_preflight._require_clean_worktree(ROOT)

    def test_branch_and_head_are_not_release_identifiers(self):
        with self.assertRaisesRegex(deployment_preflight.PreflightError, "not HEAD"):
            deployment_preflight._resolve_immutable_release(ROOT, "HEAD")

        with patch.object(
            deployment_preflight,
            "_git",
            return_value=completed(["git"], stdout="refs/heads/dev\n"),
        ):
            with self.assertRaisesRegex(deployment_preflight.PreflightError, "must not name a branch"):
                deployment_preflight._resolve_immutable_release(ROOT, "dev")

    def test_local_secret_files_are_rejected_but_templates_are_allowed(self):
        self.assertTrue(deployment_preflight._is_sensitive_tracked_path("secret_config.py"))
        self.assertTrue(deployment_preflight._is_sensitive_tracked_path("secret_config.prod.py"))
        self.assertTrue(deployment_preflight._is_sensitive_tracked_path(".env.production"))
        self.assertFalse(deployment_preflight._is_sensitive_tracked_path("secret_config.example.py"))
        self.assertFalse(deployment_preflight._is_sensitive_tracked_path(".env.example"))

    def test_clean_tagged_release_runs_offline_checks(self):
        revision = "a" * 40

        def fake_git(_root, *arguments, check=True):
            command = list(arguments)
            if command == ["rev-parse", "--is-inside-work-tree"]:
                return completed(command, stdout="true\n")
            if command == ["status", "--porcelain=v1", "--untracked-files=all"]:
                return completed(command)
            if command == ["rev-parse", "--symbolic-full-name", "v1.0"]:
                return completed(command, stdout="refs/tags/v1.0\n")
            if command in (["rev-parse", "--verify", "v1.0^{commit}"], ["rev-parse", "HEAD"]):
                return completed(command, stdout=f"{revision}\n")
            if command[:2] == ["check-ignore", "--quiet"]:
                return completed(command)
            if command == ["ls-files"]:
                return completed(command, stdout="README.md\nsecret_config.example.py\n")
            self.fail(f"unexpected git command: {command}")

        validation = type("Validation", (), {"accounts": ("hub_demo",), "strategies": ("demo",)})()
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "secret_config.py"
            with patch.object(deployment_preflight, "_git", side_effect=fake_git), patch.object(
                deployment_preflight, "_run_secret_scan"
            ), patch("shared.config_validator.validate_configuration", return_value=validation):
                report = deployment_preflight.run_preflight(
                    ROOT, release="v1.0", secret_config=secret_path
                )

        self.assertEqual(report.revision, revision)
        self.assertEqual(report.accounts, ("hub_demo",))


if __name__ == "__main__":
    unittest.main()
