"""Git-boundary checks that remain independent of any agent host."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .contract import git_output, now_iso, worktree_digest


FORBIDDEN_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"(^|/)\.dev\.vars($|\.)"),
    re.compile(r"(^|/)(?:credentials|service-account)\.json$", re.IGNORECASE),
    re.compile(r"\.(pem|p12|pfx|key)$", re.IGNORECASE),
)
SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|secret|password|access[_-]?token|_authToken)"
        r"\s*[:=]\s*['\"]"
        r"(?!example\b|placeholder\b|redacted\b|<redacted>|\{|\$)"
        r"[A-Za-z0-9_./+=-]{8,}['\"]"
    ),
    re.compile(
        r"(?i)(?:api[_-]?key|secret|password|access[_-]?token|_authToken)"
        r"\s*[:=]\s*"
        r"(?!example\b|placeholder\b|redacted\b|<redacted>|\{|\$)"
        r"(?![A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
        r"\s*(?:\r?\n|$|[#;,]))"
        r"(?!(?-i:[A-Z][A-Z0-9_]{7,})\s*(?:\r?\n|$|[#;,]))"
        r"(?=[A-Za-z0-9_./+=-]{8,}(?:\s|$))"
        r"(?=[A-Za-z0-9_./+=-]*[0-9+/=_-])"
        r"[A-Za-z0-9_./+=-]{8,}"
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


def default_base(root: Path) -> str | None:
    for candidate in ("@{upstream}", "origin/main", "origin/master"):
        code, _, _ = command_output(root, "git", "rev-parse", "--verify", candidate)
        if code == 0:
            return candidate
    return None


def outgoing_commits(
    root: Path, base: str | None = None, heads: tuple[str, ...] = ()
) -> list[str]:
    if heads:
        resolved = [git_output(root, "rev-parse", "--verify", head) for head in heads]
        return git_output(
            root, "rev-list", "--reverse", *resolved, "--not", "--remotes=origin"
        ).splitlines()
    reference = base or default_base(root)
    if reference is None:
        return git_output(
            root, "rev-list", "--reverse", "HEAD", "--not", "--remotes=origin"
        ).splitlines()
    merge_base_code, merge_base, _ = command_output(
        root, "git", "merge-base", reference, "HEAD"
    )
    start = merge_base if merge_base_code == 0 else reference
    return git_output(root, "rev-list", "--reverse", f"{start}..HEAD").splitlines()


def secret_findings(
    root: Path, base: str | None = None, heads: tuple[str, ...] = ()
) -> list[str]:
    findings: list[str] = []
    for commit in outgoing_commits(root, base, heads):
        names = git_output(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            "--root",
            "-m",
            "-r",
            commit,
        ).splitlines()
        diff = git_output(root, "show", "--format=", "--unified=0", commit)
        findings.extend(secret_findings_for_diff(commit[:12], names, diff))
    return list(dict.fromkeys(findings))


def staged_secret_findings(root: Path) -> list[str]:
    names = git_output(
        root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMRTUXB",
    ).splitlines()
    diff = git_output(root, "diff", "--cached", "--unified=0")
    return secret_findings_for_diff("staged", names, diff)


def secret_findings_for_diff(identifier: str, names: list[str], diff: str) -> list[str]:
    findings = [
        f"{identifier} forbidden file: {name}"
        for name in names
        if is_forbidden_file(name)
    ]
    added_lines = "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for pattern in SECRET_PATTERNS:
        if pattern.search(added_lines):
            findings.append(
                f"{identifier} secret-like content matched detector: {pattern.pattern}"
            )
    return findings


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
    branch = git_output(root, "branch", "--show-current")
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
    if expected_environment and not virtual_environment:
        expected_path = (
            expected_environment.resolve()
            if expected_environment.is_absolute()
            else (root / expected_environment).resolve()
        )
        failures.append(f"VIRTUAL_ENV is not active; expected {expected_path}")
    elif virtual_environment:
        environment_path = Path(virtual_environment).resolve()
        if not environment_path.exists():
            failures.append(f"VIRTUAL_ENV does not exist: {virtual_environment}")
        elif expected_environment:
            expected_path = (
                expected_environment.resolve()
                if expected_environment.is_absolute()
                else (root / expected_environment).resolve()
            )
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
        "branch": branch,
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


def protected_push_failures(
    arguments: tuple[str, ...], require_refspec: bool = True
) -> list[str]:
    failures: list[str] = []
    if any(
        argument
        in {
            "--all",
            "--branches",
            "--force",
            "--force-with-lease",
            "--mirror",
            "--prune",
            "-f",
        }
        or argument.startswith("--force-with-lease=")
        for argument in arguments
    ):
        failures.append("push option can update or delete protected remote branches")
    delete_index = next(
        (
            index
            for index, argument in enumerate(arguments)
            if argument in {"--delete", "-d"}
        ),
        None,
    )
    if delete_index is not None:
        for argument in arguments[delete_index + 1 :]:
            if protected_ref(argument):
                failures.append(f"push targets protected remote ref {argument}")
    refspecs = push_refspecs(arguments)
    if require_refspec and not refspecs and delete_index is None:
        failures.append("git push must name an explicit source refspec")
    for argument in refspecs:
        if argument == ":" or "*" in argument:
            failures.append("push uses a matching or wildcard refspec")
            continue
        destination = argument.lstrip("+").split(":", 1)[-1]
        if protected_ref(destination):
            failures.append(f"push targets protected remote ref {destination}")
    return list(dict.fromkeys(failures))


def push_sources(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if any(argument in {"--delete", "-d"} for argument in arguments):
        return ()
    sources: list[str] = []
    for refspec in push_refspecs(arguments):
        if refspec == ":" or "*" in refspec:
            continue
        normalized = refspec.lstrip("+")
        source = normalized.split(":", 1)[0]
        if source:
            sources.append(source)
    return tuple(dict.fromkeys(sources))


def push_refspecs(arguments: tuple[str, ...]) -> list[str]:
    options_with_values = {
        "--exec",
        "--push-option",
        "--receive-pack",
        "--recurse-submodules",
        "--repo",
        "-o",
    }
    positionals: list[str] = []
    skip_next = False
    skip_redirection_target = False
    repository_from_option = False
    for argument in arguments:
        if skip_redirection_target:
            skip_redirection_target = False
            continue
        if re.fullmatch(r"\d*(?:<|>|>>)", argument):
            skip_redirection_target = True
            continue
        if re.match(r"^\d*(?:<|>|>>).+", argument):
            continue
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
        if argument.startswith("--recurse-submodules="):
            continue
        if argument.startswith("-"):
            continue
        positionals.append(argument)
    refspecs = positionals if repository_from_option else positionals[1:]
    if refspecs[:1] == ["tag"]:
        return refspecs[1:]
    return refspecs


def protected_ref(reference: str) -> bool:
    normalized = reference.removeprefix("refs/heads/")
    return normalized in {"main", "master"}


def validate_git_action(
    root: Path,
    contract: dict[str, Any],
    action: str,
    arguments: tuple[str, ...] = (),
    source: str = "git",
) -> list[str]:
    failures: list[str] = []
    branch = git_output(root, "branch", "--show-current")
    if action == "pr-merge":
        return ["agents are not authorized to merge pull requests"]
    mutating_actions = {
        "branch-change",
        "commit",
        "history-write",
        "merge",
        "push",
    }
    if (
        action in mutating_actions
        and action != "branch-change"
        and branch in {"main", "master"}
    ):
        failures.append(f"{action} on protected branch {branch} is not allowed")
    if contract["terminal_action"] == "report-only":
        failures.append(f"report-only contract does not authorize {action}")
    if action in mutating_actions:
        preflight = contract.get("preflight")
        if preflight is None:
            failures.append(f"{action} requires a successful repository preflight")
        else:
            failures.extend(
                f"{action} blocked by preflight failure: {failure}"
                for failure in preflight.get("failures", [])
            )
            checked_head = preflight.get("head")
            current_head = git_output(root, "rev-parse", "HEAD")
            checked_branch = preflight.get("branch")
            current_branch = git_output(root, "branch", "--show-current")
            if checked_head != current_head or checked_branch != current_branch:
                failures.append(
                    f"{action} requires a fresh preflight for current checkout "
                    f"{current_branch or '(detached)'} at {current_head}"
                )
    if action == "commit":
        if any(
            argument in {"-a", "--all", "-i", "--include", "-o", "--only"}
            or argument.startswith(("--include=", "--only="))
            for argument in arguments
        ):
            failures.append("commit must not stage files during execution")
        failures.extend(staged_secret_findings(root))
    if action == "push":
        failures.extend(
            protected_push_failures(arguments, require_refspec=source == "git")
        )
        heads = push_sources(arguments) if source == "git" else ()
        failures.extend(secret_findings(root, heads=heads))
        validations = [
            item for item in contract["evidence"] if item.get("type") == "validation"
        ]
        if not validations:
            failures.append("push requires recorded validation evidence")
        elif validations[-1].get("worktree_digest") != worktree_digest(root):
            failures.append(
                "push requires validation evidence bound to current repository contents"
            )
    return failures
