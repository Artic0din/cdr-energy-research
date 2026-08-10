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
    complete_and_archive,
    read_contract,
    repository_root,
    state_path,
)
from .git_policy import validate_git_action


ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
STATE_CHANGING_GIT_COMMANDS = {"checkout", "reset", "restore", "switch"}
GUARDED_GIT_COMMANDS = {
    "am": "history-write",
    "cherry-pick": "history-write",
    "commit": "commit",
    "merge": "merge",
    "rebase": "history-write",
    "revert": "history-write",
    "push": "push",
}


@dataclass(frozen=True)
class DetectedGitAction:
    action: str
    arguments: tuple[str, ...] = ()
    target_root: Path | None = None
    selection_failure: str | None = None
    source: str = "git"


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
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            current.append(character)
            escaped = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            current.append(character)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(character)
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            current.append(character)
            index += 1
            continue
        boundary_length = 0
        if command[index : index + 2] in {"&&", "||"}:
            boundary_length = 2
        elif character in {";", "|", "\n"}:
            boundary_length = 1
        if boundary_length:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += boundary_length
            continue
        current.append(character)
        index += 1
    if quote or escaped:
        raise ContractError("Unparsable shell command; refusing to fail open")
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def command_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment)
    except ValueError as error:
        raise ContractError(f"Unparsable shell command: {error}") from error
    while tokens and ENVIRONMENT_ASSIGNMENT.fullmatch(tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] in {"env", "/usr/bin/env"}:
        tokens.pop(0)
        while tokens and (
            tokens[0].startswith("-") or ENVIRONMENT_ASSIGNMENT.fullmatch(tokens[0])
        ):
            tokens.pop(0)
    return tokens


def executable_name(token: str) -> str:
    return Path(token).name


def unwrap_command_prefix(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and executable_name(remaining[0]) in {"builtin", "command", "exec"}:
        remaining.pop(0)
        while remaining and remaining[0].startswith("-"):
            remaining.pop(0)
    if remaining and executable_name(remaining[0]) == "sudo":
        remaining.pop(0)
        value_options = {
            "-C",
            "-D",
            "-g",
            "-h",
            "-p",
            "-R",
            "-T",
            "-u",
            "--chdir",
            "--group",
            "--host",
            "--other-user",
            "--prompt",
            "--role",
            "--type",
            "--user",
        }
        while remaining and remaining[0].startswith("-"):
            option = remaining.pop(0)
            if option in value_options:
                if not remaining:
                    raise ContractError(f"sudo option {option} is missing its value")
                remaining.pop(0)
    return remaining


def is_bootstrap_command(command: str) -> bool:
    segments = command_segments(command)
    if len(segments) != 1 or "$(" in command or "`" in command:
        return False
    tokens = unwrap_command_prefix(command_tokens(segments[0]))
    return (
        len(tokens) >= 4
        and executable_name(tokens[0]) in {"python", "python3"}
        and tokens[1:4] == ["-m", "agent_guard", "begin"]
    ) or (len(tokens) >= 2 and pinned_launcher(tokens[0]) and tokens[1] == "begin")


def pinned_launcher(token: str) -> bool:
    normalized = token.replace("\\", "/")
    return normalized == ".agent-guard/agent-guard" or normalized.endswith(
        "/.agent-guard/agent-guard"
    )


def git_operations(
    command: str,
    root: Path | None = None,
    execution_cwd: Path | None = None,
    recursion_depth: int = 0,
) -> list[DetectedGitAction]:
    if recursion_depth > 4:
        raise ContractError("Nested shell command depth exceeds agent-guard limit")
    operations: list[DetectedGitAction] = []
    segments = command_segments(command)
    current_directory = (
        (execution_cwd or root).resolve() if (execution_cwd or root) else None
    )
    prior_state_change = False
    prior_guarded_mutation = False
    for segment in segments:
        tokens = unwrap_command_prefix(command_tokens(segment))
        if not tokens:
            continue
        if executable_name(tokens[0]) == "cd":
            directory_arguments = tokens[1:]
            if directory_arguments[:1] == ["--"]:
                directory_arguments = directory_arguments[1:]
            if len(directory_arguments) != 1 or current_directory is None:
                raise ContractError("Compound cd command cannot be resolved safely")
            current_directory = selected_git_directory(
                current_directory, directory_arguments[0]
            )
            continue
        shell_name = executable_name(tokens[0])
        if shell_name in {"bash", "sh", "zsh"}:
            command_index = next(
                (
                    index + 1
                    for index, token in enumerate(tokens[:-1])
                    if token.startswith("-") and "c" in token[1:]
                ),
                None,
            )
            if command_index is None:
                continue
            if tokens[command_index : command_index + 1] == ["--"]:
                command_index += 1
            if command_index >= len(tokens):
                raise ContractError("Shell -c option is missing its command")
            nested = git_operations(
                tokens[command_index],
                root,
                current_directory,
                recursion_depth + 1,
            )
            if prior_state_change or prior_guarded_mutation:
                nested = [with_compound_failure(operation) for operation in nested]
            operations.extend(nested)
            if nested:
                prior_guarded_mutation = True
            continue
        gh_index = gh_subcommand_index(tokens)
        if gh_index is not None and tokens[gh_index : gh_index + 2] == ["pr", "merge"]:
            operations.append(
                DetectedGitAction(
                    "pr-merge",
                    tuple(tokens[gh_index + 2 :]),
                    target_root=root,
                    source="gh",
                )
            )
            prior_guarded_mutation = True
            continue
        if (
            gh_index is not None
            and tokens[gh_index] == "api"
            and gh_api_merges(tokens[gh_index + 1 :])
        ):
            operations.append(
                DetectedGitAction(
                    "pr-merge",
                    tuple(tokens[gh_index + 1 :]),
                    target_root=root,
                    source="gh",
                )
            )
            prior_guarded_mutation = True
            continue
        if (
            len(tokens) >= 2
            and executable_name(tokens[0]) == "gt"
            and tokens[1] in {"merge", "submit"}
        ):
            operations.append(
                DetectedGitAction(
                    "pr-merge" if tokens[1] == "merge" else "push",
                    tuple(tokens[2:]),
                    target_root=root,
                    source="gt",
                )
            )
            prior_guarded_mutation = True
            continue
        if executable_name(tokens[0]) != "git":
            continue
        index = 1
        selected_directory = current_directory or root
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
        if index >= len(tokens):
            continue
        subcommand = tokens[index]
        if subcommand == "reset" and "--hard" in tokens[index + 1 :]:
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[index + 1 :]),
                    target_root=root,
                    selection_failure="git reset --hard is not authorized",
                )
            )
            prior_state_change = True
            continue
        if subcommand in STATE_CHANGING_GIT_COMMANDS:
            prior_state_change = True
            continue
        action = GUARDED_GIT_COMMANDS.get(subcommand)
        if action is None:
            continue
        if prior_state_change or prior_guarded_mutation:
            selection_failure = (
                "compound command mutates repository state before a guarded Git action; "
                "run each mutating command in a separate tool call"
            )
        target_root = root
        if root and selected_directory and not selection_failure:
            try:
                target_root = repository_root(selected_directory)
            except ContractError as error:
                selection_failure = str(error)
        operations.append(
            DetectedGitAction(
                action,
                tuple(tokens[index + 1 :]),
                target_root=target_root,
                selection_failure=selection_failure,
                source="git",
            )
        )
        prior_guarded_mutation = True
    return operations


def with_compound_failure(operation: DetectedGitAction) -> DetectedGitAction:
    return DetectedGitAction(
        operation.action,
        operation.arguments,
        operation.target_root,
        operation.selection_failure
        or (
            "compound command mutates repository state before a guarded Git action; "
            "run each mutating command in a separate tool call"
        ),
        operation.source,
    )


def gh_subcommand_index(tokens: list[str]) -> int | None:
    if not tokens or executable_name(tokens[0]) != "gh":
        return None
    index = 1
    while index < len(tokens):
        option = tokens[index]
        if option in {"-R", "--repo", "--hostname"}:
            index += 2
            continue
        if option.startswith(("--repo=", "--hostname=")):
            index += 1
            continue
        if option == "--":
            return index + 1
        if option.startswith("-"):
            index += 1
            continue
        return index
    return None


def gh_api_merges(arguments: list[str]) -> bool:
    if any("mergePullRequest" in argument for argument in arguments):
        return True
    method = "GET"
    endpoint = ""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-X", "--method"} and index + 1 < len(arguments):
            method = arguments[index + 1].upper()
            index += 2
            continue
        if argument.startswith("--method="):
            method = argument.partition("=")[2].upper()
        elif not argument.startswith("-") and not endpoint:
            endpoint = argument
        index += 1
    return (
        method in {"POST", "PUT"}
        and re.search(r"(?:^|/)pulls/\d+/merge(?:$|\?)", endpoint) is not None
    )


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
        if state_path(root).exists():
            raise
        return [
            "Initialize agent-guard before using tools: .agent-guard/agent-guard begin "
            "--terminal-action <report-only|fix-and-push|full-remediation> "
            "--deliverable '<requested outcome>'"
        ]
    failures: list[str] = []
    payload_cwd = Path(payload.get("cwd") or root).resolve()
    for operation in git_operations(command, root, payload_cwd):
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
            validate_git_action(
                root,
                contract,
                operation.action,
                operation.arguments,
                operation.source,
            )
        )
    return list(dict.fromkeys(failures))


def stop_failures(root: Path) -> list[str]:
    if not state_path(root).exists():
        return []
    failures, _ = complete_and_archive(root)
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
