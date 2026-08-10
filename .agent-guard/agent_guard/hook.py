"""Translate common agent hook payloads into agent-guard decisions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

from .contract import (
    ContractError,
    archive_contract,
    completion_failures,
    read_contract,
    repository_root,
)
from .git_policy import validate_git_action


ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
SHELL_BOUNDARY = re.compile(r"\s*(?:&&|\|\||[;|\n])\s*")


@dataclass(frozen=True)
class DetectedGitAction:
    action: str
    arguments: tuple[str, ...] = ()
    target_root: Path | None = None
    selection_failure: str | None = None


def load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise ContractError(f"Invalid hook JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ContractError("Hook payload must be a JSON object")
    return payload


def hook_root(payload: dict[str, Any]) -> Path:
    candidates = [payload.get("cwd"), *(payload.get("workspace_roots") or [])]
    for candidate in candidates:
        if candidate:
            try:
                return repository_root(Path(candidate))
            except ContractError:
                continue
    return repository_root()


def shell_command(payload: dict[str, Any]) -> str:
    direct = payload.get("command")
    if isinstance(direct, str):
        return direct
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        return command if isinstance(command, str) else ""
    return ""


def command_segments(command: str) -> list[str]:
    return [segment for segment in SHELL_BOUNDARY.split(command) if segment.strip()]


def command_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return []
    while tokens and ENVIRONMENT_ASSIGNMENT.fullmatch(tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] in {"env", "/usr/bin/env"}:
        tokens.pop(0)
        while tokens and (
            tokens[0].startswith("-") or ENVIRONMENT_ASSIGNMENT.fullmatch(tokens[0])
        ):
            tokens.pop(0)
    return tokens


def is_bootstrap_command(command: str) -> bool:
    segments = command_segments(command)
    if len(segments) != 1 or "$(" in command or "`" in command:
        return False
    tokens = command_tokens(segments[0])
    return (
        len(tokens) >= 4
        and tokens[0] in {"python", "python3"}
        and tokens[1:4] == ["-m", "agent_guard", "begin"]
    ) or (len(tokens) >= 2 and tokens[:2] == ["agent-guard", "begin"])


def git_operations(command: str, root: Path | None = None) -> list[DetectedGitAction]:
    operations: list[DetectedGitAction] = []
    for segment in command_segments(command):
        tokens = command_tokens(segment)
        if len(tokens) >= 3 and tokens[:3] == ["gh", "pr", "merge"]:
            operations.append(
                DetectedGitAction("pr-merge", tuple(tokens[3:]), target_root=root)
            )
            continue
        if len(tokens) >= 2 and tokens[:2] in (["gt", "merge"], ["gt", "submit"]):
            operations.append(
                DetectedGitAction(
                    "pr-merge" if tokens[1] == "merge" else "push",
                    tuple(tokens[2:]),
                    target_root=root,
                )
            )
            continue
        if not tokens or tokens[0] != "git":
            continue
        index = 1
        selected_directory = root
        selection_failure = None
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            index += 1
            if option == "-C":
                if index >= len(tokens):
                    selection_failure = "git -C is missing its path"
                    break
                selected_directory = selected_git_directory(
                    selected_directory, tokens[index]
                )
                index += 1
            elif option.startswith("-C") and len(option) > 2:
                selected_directory = selected_git_directory(
                    selected_directory, option[2:]
                )
            elif option in {"--git-dir", "--work-tree"} or option.startswith(
                ("--git-dir=", "--work-tree=")
            ):
                selection_failure = (
                    f"repository-selection option {option.split('=', 1)[0]} "
                    "is not supported by agent guard"
                )
                if "=" not in option:
                    index += 1
            elif option in {"-c", "--namespace"}:
                index += 1
        if index >= len(tokens) or tokens[index] not in {"commit", "push", "merge"}:
            continue
        target_root = root
        if root and selected_directory and not selection_failure:
            try:
                target_root = repository_root(selected_directory)
            except ContractError as error:
                selection_failure = str(error)
        operations.append(
            DetectedGitAction(
                tokens[index],
                tuple(tokens[index + 1 :]),
                target_root=target_root,
                selection_failure=selection_failure,
            )
        )
    return operations


def selected_git_directory(current: Path | None, value: str) -> Path | None:
    if current is None:
        return None
    candidate = Path(value)
    return (
        candidate.resolve()
        if candidate.is_absolute()
        else (current / candidate).resolve()
    )


def git_actions(command: str) -> list[str]:
    return list(dict.fromkeys(item.action for item in git_operations(command)))


def pre_tool_failures(payload: dict[str, Any], root: Path) -> list[str]:
    command = shell_command(payload)
    if command and is_bootstrap_command(command):
        return []
    try:
        contract = read_contract(root)
    except ContractError:
        return [
            "Initialize agent-guard before using tools: python3 -m agent_guard begin "
            "--terminal-action <report-only|fix-and-push|full-remediation> "
            "--deliverable '<requested outcome>'"
        ]
    failures: list[str] = []
    for operation in git_operations(command, root):
        if operation.selection_failure:
            failures.append(operation.selection_failure)
            continue
        if operation.target_root and operation.target_root.resolve() != root.resolve():
            failures.append(
                "Git action targets a different repository: "
                f"{operation.target_root}; start a contract there instead"
            )
            continue
        failures.extend(
            validate_git_action(root, contract, operation.action, operation.arguments)
        )
    return list(dict.fromkeys(failures))


def stop_failures(root: Path) -> list[str]:
    try:
        contract = read_contract(root)
    except ContractError:
        return []
    failures = completion_failures(contract, root)
    if not failures:
        archive_contract(root, contract)
    return failures


def render(host: str, event: str, failures: list[str]) -> dict[str, Any]:
    if not failures:
        if host == "cursor" and event != "stop":
            return {"permission": "allow"}
        return {}
    reason = "Agent guard blocked this action:\n" + "\n".join(
        f"- {item}" for item in failures
    )
    if host == "cursor":
        if event == "stop":
            return {"followup_message": reason}
        return {"permission": "deny", "user_message": reason, "agent_message": reason}
    if event == "stop":
        return {"decision": "block", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, choices=("claude", "codex", "cursor"))
    parser.add_argument("--event", required=True, choices=("pre-tool", "stop"))
    args = parser.parse_args(argv)
    try:
        payload = load_payload()
        try:
            root = hook_root(payload)
        except ContractError:
            print(json.dumps(render(args.host, args.event, [])))
            return 0
        failures = (
            stop_failures(root)
            if args.event == "stop"
            else pre_tool_failures(payload, root)
        )
        print(json.dumps(render(args.host, args.event, failures)))
        return 0
    except ContractError as error:
        print(json.dumps(render(args.host, args.event, [str(error)])))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
