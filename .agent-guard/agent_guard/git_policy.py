"""Git-boundary checks that remain independent of any agent host."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .contract import git_output, now_iso


FORBIDDEN_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"\.(pem|p12|pfx|key)$", re.IGNORECASE),
)
SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*"
        r"['\"](?!example|placeholder|redacted|<redacted>|\{|\$)[^'\"\s]{8,}['\"]"
    ),
)


def command_output(root: Path, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [*args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def default_base(root: Path) -> str:
    for candidate in ("@{upstream}", "origin/main", "origin/master"):
        code, _, _ = command_output(root, "git", "rev-parse", "--verify", candidate)
        if code == 0:
            return candidate
    code, _, _ = command_output(root, "git", "rev-parse", "--verify", "HEAD~1")
    if code == 0:
        return "HEAD~1"
    return git_output(root, "hash-object", "-t", "tree", "/dev/null")


def outgoing_commits(root: Path, base: str | None = None) -> list[str]:
    reference = base or default_base(root)
    merge_base_code, merge_base, _ = command_output(
        root, "git", "merge-base", reference, "HEAD"
    )
    start = merge_base if merge_base_code == 0 else reference
    return git_output(root, "rev-list", "--reverse", f"{start}..HEAD").splitlines()


def secret_findings(root: Path, base: str | None = None) -> list[str]:
    findings: list[str] = []
    for commit in outgoing_commits(root, base):
        names = git_output(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            "--root",
            "-r",
            commit,
        ).splitlines()
        for name in names:
            if is_forbidden_file(name):
                findings.append(f"{commit[:12]} forbidden file: {name}")
        diff = git_output(root, "show", "--format=", "--unified=0", commit)
        added_lines = "\n".join(
            line[1:]
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        for pattern in SECRET_PATTERNS:
            if pattern.search(added_lines):
                findings.append(
                    f"{commit[:12]} secret-like content matched detector: "
                    f"{pattern.pattern}"
                )
    return list(dict.fromkeys(findings))


def is_forbidden_file(name: str) -> bool:
    if name.endswith(".env.example"):
        return False
    return any(pattern.search(name) for pattern in FORBIDDEN_FILE_PATTERNS)


def validate_preflight(
    root: Path,
    paths: list[Path],
    expected_head: str | None = None,
    expected_remote_head: str | None = None,
    remote_ref: str | None = None,
    expected_environment: Path | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    checked_paths: list[str] = []
    for path in paths:
        resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not resolved.is_relative_to(root):
            failures.append(f"path resolves outside checkout: {resolved}")
        elif not resolved.exists():
            failures.append(f"path does not exist: {resolved}")
        checked_paths.append(str(resolved))
    head = git_output(root, "rev-parse", "HEAD")
    if expected_head and head != expected_head:
        failures.append(f"HEAD mismatch: expected {expected_head}, actual {head}")
    remote_head = None
    if expected_remote_head or remote_ref:
        if not expected_remote_head or not remote_ref:
            failures.append("remote preflight requires both expected SHA and ref")
        else:
            remote_head = resolve_remote_head(root, remote_ref, failures)
            if remote_head and remote_head != expected_remote_head:
                failures.append(
                    f"remote SHA mismatch: expected {expected_remote_head}, actual {remote_head}"
                )
    virtual_environment = os.environ.get("VIRTUAL_ENV")
    if virtual_environment:
        environment_path = Path(virtual_environment).resolve()
        if not environment_path.exists():
            failures.append(f"VIRTUAL_ENV does not exist: {virtual_environment}")
        elif expected_environment:
            expected_path = expected_environment.resolve()
            if environment_path != expected_path:
                failures.append(
                    "VIRTUAL_ENV mismatch: expected "
                    f"{expected_path}, actual {environment_path}"
                )
        elif not environment_path.is_relative_to(root):
            failures.append(
                "VIRTUAL_ENV is outside the checkout; pass --expected-environment "
                f"to authorize it: {environment_path}"
            )
    return {
        "checked_at": now_iso(),
        "head": head,
        "remote_head": remote_head,
        "paths": checked_paths,
        "virtual_environment": virtual_environment,
        "failures": failures,
    }


def resolve_remote_head(root: Path, remote_ref: str, failures: list[str]) -> str | None:
    code, output, error = command_output(root, "git", "ls-remote", "origin", remote_ref)
    if code != 0:
        failures.append(f"unable to resolve origin/{remote_ref}: {error}")
        return None
    if not output:
        failures.append(f"remote ref not found: {remote_ref}")
        return None
    return output.split()[0]


def protected_push_failures(arguments: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    if any(argument in {"--all", "--mirror", "--prune"} for argument in arguments):
        failures.append("push option can update or delete protected remote branches")
    delete_index = next(
        (index for index, argument in enumerate(arguments) if argument == "--delete"),
        None,
    )
    if delete_index is not None:
        for argument in arguments[delete_index + 1 :]:
            if protected_ref(argument):
                failures.append(f"push targets protected remote ref {argument}")
    for argument in push_refspecs(arguments):
        destination = argument.lstrip("+").split(":", 1)[-1]
        if protected_ref(destination):
            failures.append(f"push targets protected remote ref {destination}")
    return list(dict.fromkeys(failures))


def push_refspecs(arguments: tuple[str, ...]) -> list[str]:
    options_with_values = {"--exec", "--push-option", "--receive-pack", "--repo", "-o"}
    positionals: list[str] = []
    skip_next = False
    repository_from_option = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument in options_with_values:
            if argument == "--repo":
                repository_from_option = True
            skip_next = True
            continue
        if argument.startswith("--repo="):
            repository_from_option = True
            continue
        if argument.startswith("-"):
            continue
        positionals.append(argument)
    if repository_from_option:
        return positionals
    return positionals[1:] if positionals else []


def protected_ref(reference: str) -> bool:
    normalized = reference.removeprefix("refs/heads/")
    return normalized in {"main", "master"}


def validate_git_action(
    root: Path,
    contract: dict[str, Any],
    action: str,
    arguments: tuple[str, ...] = (),
) -> list[str]:
    failures: list[str] = []
    branch = git_output(root, "branch", "--show-current")
    if action == "pr-merge":
        return ["agents are not authorized to merge pull requests"]
    if action in {"commit", "push", "merge"} and branch in {"main", "master"}:
        failures.append(f"{action} on protected branch {branch} is not allowed")
    if contract["terminal_action"] == "report-only":
        failures.append(f"report-only contract does not authorize {action}")
    if action == "push":
        failures.extend(protected_push_failures(arguments))
        failures.extend(secret_findings(root))
        evidence_types = {item.get("type") for item in contract["evidence"]}
        if "validation" not in evidence_types:
            failures.append("push requires recorded validation evidence")
    return failures
