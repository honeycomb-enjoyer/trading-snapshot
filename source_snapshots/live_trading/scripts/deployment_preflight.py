"""Fail-closed, offline preflight for a staged live-trading release.

This helper deliberately does not copy a release, start MT5, stop processes,
or mutate runtime state. It proves that the already staged worktree is a
clean immutable revision and that its offline configuration/security checks
pass. See ``docs/DEPLOYMENT_CHECKLIST.md`` for the operator procedure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_HEX_REVISION = re.compile(r"[0-9a-fA-F]{7,64}")


class PreflightError(RuntimeError):
    """A release does not satisfy a safety precondition."""


@dataclass(frozen=True)
class PreflightReport:
    revision: str
    accounts: tuple[str, ...]
    strategies: tuple[str, ...]


def _run(root: Path, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise PreflightError(f"{' '.join(command)}: {detail}")
    return result


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(root, ["git", *arguments], check=check)


def _require_clean_worktree(root: Path) -> None:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()
    if status:
        raise PreflightError(
            "deployment blocked: staged release worktree is dirty; create a fresh "
            "release worktree instead of pulling or resetting an active runtime"
        )


def _resolve_immutable_release(root: Path, release: str) -> str:
    if release.upper() == "HEAD":
        raise PreflightError("--release must be an immutable tag or full/short commit SHA, not HEAD")

    symbolic = _git(root, "rev-parse", "--symbolic-full-name", release).stdout.strip()
    if symbolic.startswith("refs/heads/"):
        raise PreflightError("--release must not name a branch; use an immutable tag or commit SHA")
    if symbolic and not symbolic.startswith("refs/tags/"):
        raise PreflightError("--release must resolve to a tag or commit SHA")
    if not symbolic and not _HEX_REVISION.fullmatch(release):
        raise PreflightError("--release must be an immutable tag or commit SHA")

    expected = _git(root, "rev-parse", "--verify", f"{release}^{{commit}}").stdout.strip()
    actual = _git(root, "rev-parse", "HEAD").stdout.strip()
    if expected != actual:
        raise PreflightError(
            "deployment blocked: HEAD is not the requested release revision; "
            "stage a detached worktree at that exact revision first"
        )
    return actual


def _require_ignored_local_paths(root: Path) -> None:
    for relative_path in ("secret_config.py", "runtime/account_state.sqlite3"):
        result = _git(root, "check-ignore", "--quiet", relative_path, check=False)
        if result.returncode != 0:
            raise PreflightError(f"{relative_path} must be ignored by Git")

    tracked = _git(root, "ls-files").stdout.splitlines()
    forbidden = [path for path in tracked if _is_sensitive_tracked_path(path)]
    if forbidden:
        raise PreflightError(f"deployment blocked: sensitive path is tracked: {', '.join(forbidden)}")


def _is_sensitive_tracked_path(path: str) -> bool:
    """Reject real local secret names while allowing documented templates."""
    filename = Path(path).name.lower()
    if filename == "secret_config.py":
        return True
    if filename.startswith("secret_config.") and filename.endswith(".py"):
        return filename != "secret_config.example.py"
    if filename.startswith(".env"):
        return filename not in {".env.example", ".env.sample"}
    return filename.endswith((".pem", ".key", ".p12", ".pfx"))


def _run_secret_scan(root: Path) -> None:
    result = _run(
        root,
        [sys.executable, "-m", "detect_secrets", "scan", "--force-use-all-plugins"],
    )
    try:
        findings = json.loads(result.stdout).get("results", {})
    except json.JSONDecodeError as exc:
        raise PreflightError("detect-secrets returned invalid JSON") from exc
    if findings:
        raise PreflightError(
            "deployment blocked: detect-secrets found potential secrets in tracked files: "
            + ", ".join(sorted(findings))
        )


def run_preflight(root: Path, *, release: str, secret_config: Path) -> PreflightReport:
    """Run all read-only release checks and return only non-secret metadata."""
    root = root.resolve()
    _git(root, "rev-parse", "--is-inside-work-tree")
    _require_clean_worktree(root)
    revision = _resolve_immutable_release(root, release)
    _require_ignored_local_paths(root)
    _run_secret_scan(root)

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from shared.config_validator import ConfigurationValidationError, validate_configuration

    try:
        validation = validate_configuration(root, secret_config_path=secret_config)
    except ConfigurationValidationError as exc:
        raise PreflightError(f"configuration invalid: {exc}") from exc
    return PreflightReport(revision, validation.accounts, validation.strategies)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only deployment dry run; it never starts MT5 or changes runtime state."
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help="staged release worktree")
    parser.add_argument("--release", required=True, help="immutable Git tag or commit SHA checked out in the worktree")
    parser.add_argument(
        "--secret-config",
        type=Path,
        help="local secret_config.py path (default: <root>/secret_config.py)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    secret_config = (args.secret_config or root / "secret_config.py").resolve()
    try:
        report = run_preflight(root, release=args.release, secret_config=secret_config)
    except PreflightError as exc:
        print(f"DEPLOYMENT DRY RUN FAILED: {exc}", file=sys.stderr)
        return 1

    print("DEPLOYMENT DRY RUN PASSED (no MT5/process/runtime-state changes were made)")
    print(f"revision: {report.revision}")
    print(f"accounts: {', '.join(report.accounts)}")
    print(f"strategies: {', '.join(report.strategies)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
