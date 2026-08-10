"""Translate common agent hook payloads into agent-guard decisions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any

from .contract import (
    ContractError,
    aborted_path,
    complete_and_archive,
    git_output,
    read_contract,
    repository_root,
    state_path,
)
from .git_policy import validate_git_action


ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
GUARDED_GIT_COMMANDS = {
    "add": "history-write",
    "am": "history-write",
    "cherry-pick": "history-write",
    "commit": "commit",
    "merge": "merge",
    "mv": "history-write",
    "pull": "history-write",
    "rebase": "history-write",
    "reset": "history-write",
    "restore": "history-write",
    "revert": "history-write",
    "rm": "history-write",
    "push": "push",
    "send-pack": "push",
    "update-ref": "history-write",
}
BRANCH_CHANGE_COMMANDS = {"checkout", "switch"}
HEREDOC = re.compile(
    r"<<(?P<tabs>-)?\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=quote)"
)
SHELL_CONTROL_WORDS = {
    "!",
    "{",
    "}",
    "do",
    "elif",
    "else",
    "if",
    "then",
    "until",
    "while",
}
BRANCH_MUTATION_OPTIONS = {
    "-c",
    "-C",
    "-d",
    "-D",
    "-m",
    "-M",
    "--copy",
    "--delete",
    "--move",
}
CHECKOUT_SWITCH_MUTATION_OPTIONS = {
    "-b",
    "-B",
    "-c",
    "-C",
    "--orphan",
    "--create",
    "--force-create",
}
GH_APPROVED_TOP_LEVEL_COMMANDS = {
    "alias",
    "api",
    "auth",
    "aw",
    "browse",
    "cache",
    "codespace",
    "completion",
    "config",
    "extension",
    "gist",
    "gpg-key",
    "issue",
    "label",
    "org",
    "pr",
    "project",
    "release",
    "repo",
    "ruleset",
    "run",
    "search",
    "secret",
    "ssh-key",
    "status",
    "variable",
    "workflow",
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
    workspace_roots = payload.get("workspace_roots") or []
    if not isinstance(workspace_roots, list):
        raise ContractError("Hook workspace_roots must be a list")
    payload_cwd = payload.get("cwd")
    if payload_cwd:
        if not isinstance(payload_cwd, str):
            raise ContractError("Hook repository paths must be text")
        return repository_root(Path(payload_cwd))
    for candidate in workspace_roots:
        if candidate:
            if not isinstance(candidate, str):
                raise ContractError("Hook repository paths must be text")
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
    command = without_heredoc_bodies(command)
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
        if character == "#" and (
            index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&()"
        ):
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        boundary_length = 0
        if command[index : index + 2] in {"&&", "||"}:
            boundary_length = 2
        elif character in {";", "|", "\n"} or (
            character == "&"
            and command[index - 1 : index] != ">"
            and command[index + 1 : index + 2] != ">"
        ):
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


def without_heredoc_bodies(command: str) -> str:
    retained: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delimiter:
                pending.pop(0)
            continue
        retained.append(line)
        pending.extend(heredoc_markers(line))
    if pending:
        raise ContractError("Unterminated shell heredoc; refusing to fail open")
    return "".join(retained)


def heredoc_markers(line: str) -> list[tuple[str, bool]]:
    markers: list[tuple[str, bool]] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(line):
        character = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "#" and (
            index == 0 or line[index - 1].isspace() or line[index - 1] in ";|&()"
        ):
            break
        match = HEREDOC.match(line, index)
        if match is not None:
            markers.append((match.group("delimiter"), bool(match.group("tabs"))))
            index = match.end()
            continue
        index += 1
    return markers


def command_tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment)
    except ValueError as error:
        raise ContractError(f"Unparsable shell command: {error}") from error
    tokens = unwrap_command_prefix(tokens)
    while tokens and ENVIRONMENT_ASSIGNMENT.fullmatch(tokens[0]):
        tokens.pop(0)
    if tokens and executable_name(tokens[0]) == "env":
        tokens.pop(0)
        while tokens:
            token = tokens[0]
            if ENVIRONMENT_ASSIGNMENT.fullmatch(token):
                tokens.pop(0)
                continue
            if token in {"-C", "--chdir"} or token.startswith("--chdir="):
                raise ContractError("env directory changes are not supported")
            if token in {"-u", "--unset"}:
                if len(tokens) < 2:
                    raise ContractError(f"env option {token} is missing its value")
                del tokens[:2]
                continue
            if token.startswith("--unset=") or token in {
                "-0",
                "-i",
                "-v",
                "--debug",
                "--ignore-environment",
                "--null",
            }:
                tokens.pop(0)
                continue
            if token == "--":
                tokens.pop(0)
                break
            if token.startswith("-"):
                raise ContractError(f"unsupported env option: {token}")
            break
    return tokens


def has_unquoted_redirection(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for character in command:
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in {"<", ">"}:
            return True
    return False


def has_unquoted_git_subshell(command: str) -> bool:
    quote: str | None = None
    escaped = False
    depth = 0
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "#" and (
            index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&()"
        ):
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if character == "(":
            depth += 1
            index += 1
            continue
        if character == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth and re.match(
            r"(?:[^\s;&|()]+/)?(?:git|gh|gt)(?=\s|$|[;&|()])",
            command[index:],
        ):
            return True
        index += 1
    return False


def strip_control_words(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and remaining[0] in SHELL_CONTROL_WORDS:
        remaining.pop(0)
    return remaining


def git_subcommand_action(subcommand: str, arguments: list[str]) -> str | None:
    if subcommand == "add" and any(
        argument in {"-n", "--dry-run"} for argument in arguments
    ):
        return None
    if subcommand == "config":
        return None if config_is_read_only(arguments) else "history-write"
    if subcommand == "stash":
        return None if arguments[:1] in (["list"], ["show"]) else "history-write"
    if subcommand in BRANCH_CHANGE_COMMANDS:
        if any(
            argument in CHECKOUT_SWITCH_MUTATION_OPTIONS
            or argument.startswith(("--orphan=", "--create=", "--force-create="))
            or (len(argument) > 2 and argument[:2] in {"-b", "-B", "-c", "-C"})
            for argument in arguments
        ):
            return "history-write"
        return "branch-change"
    if subcommand == "branch":
        if any(argument in BRANCH_MUTATION_OPTIONS for argument in arguments):
            return "history-write"
        if arguments and not any(
            argument
            in {
                "--all",
                "--contains",
                "--list",
                "--merged",
                "--no-merged",
                "--remotes",
                "--show-current",
                "--verbose",
                "-a",
                "-l",
                "-r",
                "-v",
                "-vv",
            }
            for argument in arguments
        ):
            return "history-write"
        return None
    if subcommand == "tag":
        if tag_is_read_only(arguments):
            return None
        return "history-write"
    if subcommand == "clean" and any(
        argument in {"-n", "--dry-run"} for argument in arguments
    ):
        return None
    if subcommand == "clean":
        return "history-write"
    return GUARDED_GIT_COMMANDS.get(subcommand)


def config_is_read_only(arguments: list[str]) -> bool:
    mutation_options = {
        "--add",
        "--edit",
        "--remove-section",
        "--rename-section",
        "--replace-all",
        "--unset",
        "--unset-all",
        "-e",
    }
    if any(
        argument in mutation_options
        or argument.startswith(
            (
                "--add=",
                "--remove-section=",
                "--rename-section=",
                "--replace-all=",
                "--unset=",
                "--unset-all=",
            )
        )
        for argument in arguments
    ):
        return False
    query_options = {
        "--get",
        "--get-all",
        "--get-color",
        "--get-colorbool",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "--name-only",
        "-l",
    }
    if any(argument in query_options for argument in arguments):
        return True
    value_options = {
        "--default",
        "--expiry-date",
        "--type",
    }
    positionals: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument in value_options:
            skip_next = True
            continue
        if argument.startswith("-"):
            continue
        positionals.append(argument)
    return len(positionals) <= 1


def executable_shell_heredoc(command: str) -> bool:
    for line in command.splitlines():
        if not heredoc_markers(line):
            continue
        tokens = strip_control_words(unwrap_command_prefix(command_tokens(line)))
        if tokens and executable_name(tokens[0]) in {"bash", "sh", "zsh"}:
            return True
    return False


def tag_is_read_only(arguments: list[str]) -> bool:
    if not arguments:
        return True
    mutating = {
        "-a",
        "--annotate",
        "-d",
        "--delete",
        "-f",
        "--force",
        "-s",
        "--sign",
        "-u",
        "--local-user",
    }
    if any(argument in mutating for argument in arguments):
        return False
    if arguments[0] in {"-v", "--verify"}:
        return len(arguments) >= 2
    value_options = {
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--sort",
        "--format",
        "--color",
    }
    value_prefixes = tuple(f"{option}=" for option in value_options)
    list_mode = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-l", "--list"}:
            list_mode = True
            index += 1
            continue
        if argument == "-n" or re.fullmatch(r"-n\d+", argument):
            list_mode = True
            index += 1
            continue
        if argument in value_options:
            if index + 1 >= len(arguments):
                return False
            list_mode = True
            index += 2
            continue
        if argument.startswith(value_prefixes):
            list_mode = True
            index += 1
            continue
        if argument.startswith("-"):
            return False
        if not list_mode:
            return False
        index += 1
    return list_mode


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


def is_bootstrap_command(command: str, root: Path | None = None) -> bool:
    return is_guard_command(command, "begin", root)


def is_preflight_command(command: str, root: Path | None = None) -> bool:
    return is_guard_command(command, "preflight", root)


def is_guard_command(command: str, subcommand: str, root: Path | None = None) -> bool:
    segments = command_segments(command)
    if (
        len(segments) != 1
        or "$(" in command
        or "`" in command
        or has_unquoted_redirection(command)
    ):
        return False
    tokens = unwrap_command_prefix(command_tokens(segments[0]))
    return (
        len(tokens) >= 4
        and executable_name(tokens[0]) in {"python", "python3"}
        and tokens[1:4] == ["-m", "agent_guard", subcommand]
    ) or (
        len(tokens) >= 2
        and pinned_launcher(tokens[0], root)
        and tokens[1] == subcommand
    )


def pinned_launcher(token: str, root: Path | None = None) -> bool:
    normalized = token.replace("\\", "/")
    if root is None:
        return normalized == ".agent-guard/agent-guard"
    candidate = Path(token)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    )
    return resolved == (root / ".agent-guard" / "agent-guard").resolve()


def git_operations(
    command: str,
    root: Path | None = None,
    execution_cwd: Path | None = None,
    recursion_depth: int = 0,
) -> list[DetectedGitAction]:
    if recursion_depth > 4:
        raise ContractError("Nested shell command depth exceeds agent-guard limit")
    if executable_shell_heredoc(command):
        raise ContractError(
            "executable shell heredocs are not supported by agent guard"
        )
    if ("$(" in command or "`" in command) and re.search(
        r"(?:^|[^A-Za-z0-9_-])(?:git|gh|gt)(?:\s|$)", command
    ):
        raise ContractError(
            "shell command substitutions containing git, gh, or gt are not supported"
        )
    operations: list[DetectedGitAction] = []
    shell_syntax = without_heredoc_bodies(command)
    if has_unquoted_git_subshell(shell_syntax):
        raise ContractError(
            "shell subshell grouping containing git, gh, or gt is not supported"
        )
    segments = command_segments(command)
    current_directory = (
        (execution_cwd or root).resolve() if (execution_cwd or root) else None
    )
    prior_state_change = False
    prior_guarded_mutation = False
    for segment in segments:
        tokens = strip_control_words(unwrap_command_prefix(command_tokens(segment)))
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
                    if token.startswith("-")
                    and not token.startswith("--")
                    and "c" in token[1:]
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
        if gh_index is not None and tokens[gh_index : gh_index + 2] == ["pr", "create"]:
            operations.append(
                DetectedGitAction(
                    "push",
                    tuple(tokens[gh_index + 2 :]),
                    target_root=root,
                    source="gh",
                )
            )
            prior_guarded_mutation = True
            continue
        if gh_index is not None and tokens[gh_index] == "api":
            api_action = gh_api_action(tokens[gh_index + 1 :], current_directory)
            if api_action is None:
                continue
            operations.append(
                DetectedGitAction(
                    api_action,
                    tuple(tokens[gh_index + 1 :]),
                    target_root=root,
                    source="gh",
                )
            )
            prior_guarded_mutation = True
            continue
        if gh_index is not None and tokens[gh_index] == "alias":
            if tokens[gh_index + 1 : gh_index + 2] != ["list"]:
                operations.append(
                    DetectedGitAction(
                        "history-write",
                        tuple(tokens[gh_index + 1 :]),
                        target_root=root,
                        selection_failure=(
                            "GitHub CLI alias mutation is not supported by agent guard"
                        ),
                        source="gh",
                    )
                )
                prior_state_change = True
            continue
        if (
            gh_index is not None
            and tokens[gh_index] not in GH_APPROVED_TOP_LEVEL_COMMANDS
        ):
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[gh_index + 1 :]),
                    target_root=root,
                    selection_failure=(
                        "GitHub CLI aliases are not supported by agent guard"
                    ),
                    source="gh",
                )
            )
            prior_state_change = True
            continue
        if (
            len(tokens) >= 2
            and executable_name(tokens[0]) == "gt"
            and tokens[1] in {"merge", "submit"}
        ):
            target_root = root
            target_failure = None
            if current_directory:
                try:
                    target_root = repository_root(current_directory)
                except ContractError as error:
                    target_failure = str(error)
            operations.append(
                DetectedGitAction(
                    "pr-merge" if tokens[1] == "merge" else "push",
                    tuple(tokens[2:]),
                    target_root=target_root,
                    selection_failure=target_failure,
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
        alias_override = False
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
            elif option == "-c":
                if index < len(tokens) and tokens[index].startswith("alias."):
                    alias_override = True
                index += 1
            elif option.startswith("-c") and len(option) > 2:
                if option[2:].startswith("alias."):
                    alias_override = True
            elif option.startswith("--config-env"):
                selection_failure = (
                    "Git configuration environment overrides are not supported by "
                    "agent guard"
                )
            elif option == "--namespace":
                index += 1
        if index >= len(tokens):
            continue
        subcommand = tokens[index]
        if alias_override:
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[index + 1 :]),
                    target_root=root,
                    selection_failure="Git aliases are not supported by agent guard",
                )
            )
            prior_state_change = True
            continue
        if subcommand == "reset":
            reset_failure = (
                "git reset --hard is not authorized"
                if "--hard" in tokens[index + 1 :]
                else "git reset is not authorized; use git restore"
            )
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[index + 1 :]),
                    target_root=root,
                    selection_failure=reset_failure,
                )
            )
            prior_state_change = True
            continue
        if (
            subcommand == "clean"
            and git_subcommand_action(subcommand, tokens[index + 1 :]) is not None
        ):
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[index + 1 :]),
                    target_root=root,
                    selection_failure="git clean is not authorized",
                )
            )
            prior_state_change = True
            continue
        if (
            subcommand in {"config", "stash"}
            and git_subcommand_action(subcommand, tokens[index + 1 :]) is not None
        ):
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[index + 1 :]),
                    target_root=root,
                    selection_failure=f"git {subcommand} mutation is not authorized",
                )
            )
            prior_state_change = True
            continue
        action = git_subcommand_action(subcommand, tokens[index + 1 :])
        if action is None and selection_failure:
            operations.append(
                DetectedGitAction(
                    "history-write",
                    tuple(tokens[index + 1 :]),
                    target_root=root,
                    selection_failure=selection_failure,
                )
            )
            prior_state_change = True
            continue
        if action is None:
            if root and selected_directory:
                alias_code = subprocess.run(
                    ["git", "config", "--get", f"alias.{subcommand}"],
                    cwd=selected_directory,
                    text=True,
                    capture_output=True,
                    check=False,
                ).returncode
                if alias_code == 0:
                    operations.append(
                        DetectedGitAction(
                            "history-write",
                            tuple(tokens[index + 1 :]),
                            target_root=root,
                            selection_failure=(
                                "Git aliases are not supported by agent guard"
                            ),
                        )
                    )
            continue
        protected_failure = protected_history_write_failure(
            subcommand, tokens[index + 1 :]
        )
        if protected_failure:
            selection_failure = protected_failure
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
        if action in {"branch-change", "history-write"}:
            prior_state_change = True
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


def protected_history_write_failure(
    subcommand: str, arguments: list[str]
) -> str | None:
    if subcommand not in {"branch", "checkout", "switch", "update-ref"}:
        return None
    protected = {"main", "master", "refs/heads/main", "refs/heads/master"}
    if any(argument.lstrip("+") in protected for argument in arguments):
        return f"git {subcommand} cannot update a protected branch"
    return None


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


def gh_api_action(arguments: list[str], cwd: Path | None = None) -> str | None:
    merge_mutations = {
        "addPullRequestToMergeQueue",
        "enablePullRequestAutoMerge",
        "enqueuePullRequest",
        "mergePullRequest",
    }
    if any(
        mutation in argument for argument in arguments for mutation in merge_mutations
    ):
        return "pr-merge"
    method = "GET"
    method_is_explicit = False
    endpoint = ""
    payloads: list[str] = []
    value_options = {
        "-F",
        "--field",
        "-f",
        "--raw-field",
        "-H",
        "--header",
        "--hostname",
        "--input",
        "--cache",
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-X", "--method"} and index + 1 < len(arguments):
            method = arguments[index + 1].upper()
            method_is_explicit = True
            index += 2
            continue
        if argument.startswith("-X") and len(argument) > 2:
            method = argument[2:].upper()
            method_is_explicit = True
        elif argument.startswith("--method="):
            method = argument.partition("=")[2].upper()
            method_is_explicit = True
        elif argument in value_options:
            if index + 1 >= len(arguments):
                raise ContractError(f"gh api option {argument} is missing its value")
            value = arguments[index + 1]
            if argument in {"-F", "--field", "-f", "--raw-field"}:
                payloads.append(value)
            if (
                argument in {"-F", "--field", "-f", "--raw-field"}
                and not method_is_explicit
            ):
                method = "POST"
            elif argument == "--input":
                payloads.append(f"@{value}")
                if not method_is_explicit:
                    method = "POST"
            index += 2
            continue
        elif argument.startswith("--field="):
            payloads.append(argument.partition("=")[2])
            if not method_is_explicit:
                method = "POST"
        elif argument.startswith("--raw-field="):
            payloads.append(argument.partition("=")[2])
            if not method_is_explicit:
                method = "POST"
        elif (argument.startswith("-F") or argument.startswith("-f")) and len(
            argument
        ) > 2:
            payloads.append(argument[2:])
            if not method_is_explicit:
                method = "POST"
        elif argument.startswith("--input="):
            payloads.append(f"@{argument.partition('=')[2]}")
            if not method_is_explicit:
                method = "POST"
        elif not argument.startswith("-") and not endpoint:
            endpoint = argument
        index += 1
    if endpoint == "graphql":
        graphql_texts, opaque = graphql_payload_texts(payloads, cwd)
        combined = "\n".join(graphql_texts)
        if opaque or any(mutation in combined for mutation in merge_mutations):
            return "pr-merge"
        if re.search(r"\bmutation\b", combined):
            return "history-write"
        return None
    if method not in {"DELETE", "PATCH", "POST", "PUT"}:
        return None
    if "$" in endpoint or "`" in endpoint:
        return "pr-merge"
    if re.search(
        r"(?:^|/)(?:pulls/\d+/merge|merges)(?:$|\?)",
        endpoint,
    ):
        return "pr-merge"
    if re.search(r"(?:^|/)git/(?:refs|tags)(?:/|$|\?)", endpoint):
        return "pr-merge"
    return "history-write"


def gh_api_merges(arguments: list[str], cwd: Path | None = None) -> bool:
    return gh_api_action(arguments, cwd) == "pr-merge"


def graphql_payload_texts(
    values: list[str], cwd: Path | None
) -> tuple[list[str], bool]:
    texts: list[str] = []
    opaque = False
    for value in values:
        candidate = value.partition("=")[2] if "=" in value else value
        if not candidate.startswith("@"):
            texts.append(candidate)
            continue
        filename = candidate[1:]
        if not filename or filename == "-":
            opaque = True
            continue
        path = Path(filename)
        path = path if path.is_absolute() else (cwd or Path.cwd()) / path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ContractError(
                f"Unable to inspect gh api payload {path}: {error}"
            ) from error
        texts.append(content)
    return texts, opaque


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
    if command and (
        is_bootstrap_command(command, root) or is_preflight_command(command, root)
    ):
        return []
    try:
        contract = read_contract(root)
    except ContractError:
        if state_path(root).exists():
            raise
        return [
            "Initialize agent-guard before using tools: .agent-guard/agent-guard begin "
            "--terminal-action "
            "<report-only|safe-output-delivery|fix-and-push|full-remediation> "
            "--deliverable '<requested outcome>'"
        ]
    failures: list[str] = []
    preflight = contract.get("preflight")
    if preflight is None:
        failures.append("Run agent-guard preflight before using repository tools")
    else:
        failures.extend(
            f"agent-guard preflight failed: {failure}"
            for failure in preflight.get("failures", [])
        )
        current_head = git_output(root, "rev-parse", "HEAD")
        current_branch = git_output(root, "branch", "--show-current")
        if (
            preflight.get("head") != current_head
            or preflight.get("branch") != current_branch
        ):
            failures.append(
                "agent-guard preflight is stale for current checkout "
                f"{current_branch or '(detached)'} at {current_head}"
            )
    payload_cwd_value = payload.get("cwd")
    if payload_cwd_value is not None and not isinstance(payload_cwd_value, str):
        raise ContractError("hook payload cwd must be text")
    payload_cwd = Path(payload_cwd_value or root).resolve()
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
        if aborted_path(root).is_file():
            return ["aborted run has no successor contract"]
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
        root = hook_root(payload)
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
