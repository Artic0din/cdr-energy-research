"""Command-line interface for the agent-neutral execution guard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Callable

from .contract import (
    ContractError,
    abort_contract,
    add_delegate,
    archive_contract,
    completion_failures,
    create_contract,
    read_contract,
    record_evidence,
    repository_root,
    set_delegate_status,
    set_deliverable_status,
    state_path,
    write_contract,
)
from .git_policy import secret_findings, validate_git_action, validate_preflight


Command = Callable[[argparse.Namespace], int]


def load(args: argparse.Namespace) -> tuple[Path, dict]:
    root = repository_root(Path(args.cwd) if args.cwd else None)
    return root, read_contract(root)


def cmd_begin(args: argparse.Namespace) -> int:
    root = repository_root(Path(args.cwd) if args.cwd else None)
    path = state_path(root)
    if path.exists():
        raise ContractError(
            "An active contract already exists; finish or close it first"
        )
    contract = create_contract(root, args.terminal_action, args.deliverable)
    write_contract(root, contract)
    print(json.dumps(contract, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _, contract = load(args)
    print(json.dumps(contract, indent=2))
    return 0


def cmd_deliverable(args: argparse.Namespace) -> int:
    root, contract = load(args)
    set_deliverable_status(contract, args.id, args.status, args.evidence)
    write_contract(root, contract)
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    root, contract = load(args)
    record_evidence(contract, args.type, args.value)
    write_contract(root, contract)
    return 0


def cmd_add_delegate(args: argparse.Namespace) -> int:
    root, contract = load(args)
    add_delegate(contract, args.id, args.scope)
    write_contract(root, contract)
    return 0


def cmd_delegate_result(args: argparse.Namespace) -> int:
    root, contract = load(args)
    set_delegate_status(contract, args.id, args.status, args.evidence)
    write_contract(root, contract)
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    root, contract = load(args)
    result = validate_preflight(
        root,
        [Path(path) for path in args.path],
        args.expected_head,
        args.expected_remote_head,
        args.remote_ref,
        Path(args.expected_environment) if args.expected_environment else None,
    )
    contract["preflight"] = result
    write_contract(root, contract)
    print(json.dumps(result, indent=2))
    return 1 if result["failures"] else 0


def cmd_check_complete(args: argparse.Namespace) -> int:
    root, contract = load(args)
    failures = completion_failures(contract, root)
    print_result(failures, "Run contract is complete")
    return 1 if failures else 0


def cmd_check_action(args: argparse.Namespace) -> int:
    root, contract = load(args)
    failures = validate_git_action(root, contract, args.action)
    print_result(failures, f"{args.action} is allowed")
    return 1 if failures else 0


def cmd_scan_outgoing(args: argparse.Namespace) -> int:
    root = repository_root(Path(args.cwd) if args.cwd else None)
    failures = secret_findings(root, args.base)
    print_result(failures, "Outgoing commits contain no detected secrets")
    return 1 if failures else 0


def cmd_close(args: argparse.Namespace) -> int:
    root, contract = load(args)
    failures = completion_failures(contract, root)
    if failures:
        print_result(failures, "")
        return 1
    archive = archive_contract(root, contract)
    print(f"Archived completed contract at {archive}")
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    root, contract = load(args)
    archive = abort_contract(root, contract, args.reason)
    print(f"Archived aborted contract at {archive}")
    return 0


def print_result(failures: list[str], success: str) -> None:
    if not failures:
        print(f"PASS: {success}")
        return
    print("BLOCKED:")
    for failure in failures:
        print(f"- {failure}")


def add_common_cwd(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", help="Path inside the target repository")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-guard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    begin = subparsers.add_parser("begin")
    add_common_cwd(begin)
    begin.add_argument(
        "--terminal-action",
        required=True,
        choices=("report-only", "fix-and-push", "full-remediation"),
    )
    begin.add_argument("--deliverable", action="append", required=True)
    begin.set_defaults(handler=cmd_begin)
    show = subparsers.add_parser("show")
    add_common_cwd(show)
    show.set_defaults(handler=cmd_show)
    deliverable = subparsers.add_parser("set-deliverable")
    add_common_cwd(deliverable)
    deliverable.add_argument("--id", required=True)
    deliverable.add_argument(
        "--status",
        required=True,
        choices=("pending", "completed", "blocked"),
    )
    deliverable.add_argument("--evidence", default="")
    deliverable.set_defaults(handler=cmd_deliverable)
    evidence = subparsers.add_parser("record-evidence")
    add_common_cwd(evidence)
    evidence.add_argument("--type", required=True)
    evidence.add_argument("--value", required=True)
    evidence.set_defaults(handler=cmd_evidence)
    delegate = subparsers.add_parser("add-delegate")
    add_common_cwd(delegate)
    delegate.add_argument("--id", required=True)
    delegate.add_argument("--scope", required=True)
    delegate.set_defaults(handler=cmd_add_delegate)
    result = subparsers.add_parser("set-delegate-result")
    add_common_cwd(result)
    result.add_argument("--id", required=True)
    result.add_argument(
        "--status", required=True, choices=("findings", "no-findings", "blocked")
    )
    result.add_argument("--evidence", required=True)
    result.set_defaults(handler=cmd_delegate_result)
    preflight = subparsers.add_parser("preflight")
    add_common_cwd(preflight)
    preflight.add_argument("--path", action="append", default=[])
    preflight.add_argument("--expected-head")
    preflight.add_argument("--expected-remote-head")
    preflight.add_argument("--remote-ref")
    preflight.add_argument("--expected-environment")
    preflight.set_defaults(handler=cmd_preflight)
    complete = subparsers.add_parser("check-complete")
    add_common_cwd(complete)
    complete.set_defaults(handler=cmd_check_complete)
    action = subparsers.add_parser("check-action")
    add_common_cwd(action)
    action.add_argument("action", choices=("commit", "push", "merge"))
    action.set_defaults(handler=cmd_check_action)
    scan = subparsers.add_parser("scan-outgoing")
    add_common_cwd(scan)
    scan.add_argument("--base")
    scan.set_defaults(handler=cmd_scan_outgoing)
    close = subparsers.add_parser("close")
    add_common_cwd(close)
    close.set_defaults(handler=cmd_close)
    abort = subparsers.add_parser("abort")
    add_common_cwd(abort)
    abort.add_argument("--reason", required=True)
    abort.set_defaults(handler=cmd_abort)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.handler(args)
    except ContractError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
