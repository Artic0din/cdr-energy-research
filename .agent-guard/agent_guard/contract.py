"""Run-contract persistence and deterministic completion validation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1
TERMINAL_ACTIONS = ("report-only", "fix-and-push", "full-remediation")
DELIVERABLE_STATUSES = ("pending", "completed", "blocked")
DELEGATE_STATUSES = ("pending", "findings", "no-findings", "blocked")
REQUIRED_EVIDENCE = {
    "report-only": ("coverage",),
    "fix-and-push": ("validation", "push"),
    "full-remediation": ("validation", "push", "review-threads", "ci"),
}


class ContractError(RuntimeError):
    """Raised when a run contract is missing or invalid."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise ContractError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def repository_root(cwd: Path | None = None) -> Path:
    start = (cwd or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ContractError(f"Not inside a Git repository: {start}")
    return Path(result.stdout.strip()).resolve()


def state_path(root: Path) -> Path:
    relative = git_output(root, "rev-parse", "--git-path", "agent-guard/contract.json")
    path = Path(relative)
    return path if path.is_absolute() else root / path


def read_contract(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        raise ContractError("No active agent-guard contract")
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"Unable to read contract: {error}") from error
    validate_shape(contract)
    return contract


def write_contract(root: Path, contract: dict[str, Any]) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def archive_contract(root: Path, contract: dict[str, Any]) -> Path:
    path = state_path(root)
    archive = path.parent / "history" / f"{contract['run_id']}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    path.replace(archive)
    return archive


def abort_contract(root: Path, contract: dict[str, Any], reason: str) -> Path:
    if not reason.strip():
        raise ContractError("Aborting a contract requires an exact reason")
    contract["aborted_at"] = now_iso()
    contract["abort_reason"] = reason.strip()
    write_contract(root, contract)
    return archive_contract(root, contract)


def validate_shape(contract: dict[str, Any]) -> None:
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("Unsupported run-contract schema version")
    if contract.get("terminal_action") not in TERMINAL_ACTIONS:
        raise ContractError("Invalid terminal action")
    if (
        not isinstance(contract.get("deliverables"), list)
        or not contract["deliverables"]
    ):
        raise ContractError("A run contract requires at least one deliverable")
    if not isinstance(contract.get("delegates"), list):
        raise ContractError("Contract delegates must be a list")
    if not isinstance(contract.get("evidence"), list):
        raise ContractError("Contract evidence must be a list")


def create_contract(
    root: Path, terminal_action: str, descriptions: list[str]
) -> dict[str, Any]:
    if terminal_action not in TERMINAL_ACTIONS:
        raise ContractError(f"Unknown terminal action: {terminal_action}")
    cleaned = [
        description.strip() for description in descriptions if description.strip()
    ]
    if not cleaned:
        raise ContractError("At least one non-empty deliverable is required")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid4()),
        "created_at": now_iso(),
        "repository_root": str(root),
        "baseline_head": git_output(root, "rev-parse", "HEAD"),
        "baseline_branch": git_output(root, "branch", "--show-current"),
        "baseline_status": git_output(root, "status", "--porcelain=v1").splitlines(),
        "terminal_action": terminal_action,
        "required_evidence": list(REQUIRED_EVIDENCE[terminal_action]),
        "deliverables": [
            {
                "id": f"D{index}",
                "description": description,
                "status": "pending",
                "evidence": [],
            }
            for index, description in enumerate(cleaned, start=1)
        ],
        "delegates": [],
        "evidence": [],
        "preflight": None,
    }


def find_item(items: list[dict[str, Any]], item_id: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise ContractError(f"Unknown {kind} id: {item_id}")


def set_deliverable_status(
    contract: dict[str, Any], item_id: str, status: str, evidence: str
) -> None:
    if status not in DELIVERABLE_STATUSES:
        raise ContractError(f"Invalid deliverable status: {status}")
    if status != "pending" and not evidence.strip():
        raise ContractError("Terminal deliverable status requires exact evidence")
    item = find_item(contract["deliverables"], item_id, "deliverable")
    item["status"] = status
    item["evidence"] = [evidence.strip()] if evidence.strip() else []
    item["updated_at"] = now_iso()


def add_delegate(contract: dict[str, Any], delegate_id: str, scope: str) -> None:
    if any(item.get("id") == delegate_id for item in contract["delegates"]):
        raise ContractError(f"Duplicate delegate id: {delegate_id}")
    if not delegate_id.strip() or not scope.strip():
        raise ContractError("Delegate id and scope are required")
    contract["delegates"].append(
        {
            "id": delegate_id.strip(),
            "scope": scope.strip(),
            "status": "pending",
            "evidence": [],
        }
    )


def set_delegate_status(
    contract: dict[str, Any], delegate_id: str, status: str, evidence: str
) -> None:
    if status not in DELEGATE_STATUSES or status == "pending":
        raise ContractError(f"Invalid terminal delegate status: {status}")
    if not evidence.strip():
        raise ContractError(
            "Delegate result requires findings, no-findings proof, or a blocker"
        )
    item = find_item(contract["delegates"], delegate_id, "delegate")
    item["status"] = status
    item["evidence"] = [evidence.strip()]
    item["updated_at"] = now_iso()


def record_evidence(contract: dict[str, Any], evidence_type: str, value: str) -> None:
    if not evidence_type.strip() or not value.strip():
        raise ContractError("Evidence type and value are required")
    contract["evidence"].append(
        {
            "type": evidence_type.strip(),
            "value": value.strip(),
            "recorded_at": now_iso(),
        }
    )


def completion_failures(contract: dict[str, Any], root: Path) -> list[str]:
    failures: list[str] = []
    for item in contract["deliverables"]:
        if item.get("status") == "pending":
            failures.append(f"{item['id']} is still pending: {item['description']}")
        elif not item.get("evidence"):
            failures.append(f"{item['id']} has no evidence")
    for item in contract["delegates"]:
        if item.get("status") == "pending":
            failures.append(f"delegate {item['id']} has not returned a result")
        elif not item.get("evidence"):
            failures.append(f"delegate {item['id']} has no result evidence")
    all_completed = all(
        item.get("status") == "completed" for item in contract["deliverables"]
    )
    if all_completed:
        evidence_types = {item.get("type") for item in contract["evidence"]}
        for required in contract["required_evidence"]:
            if required not in evidence_types:
                failures.append(f"missing required {required} evidence")
    if contract["terminal_action"] == "report-only":
        current_head = git_output(root, "rev-parse", "HEAD")
        if current_head != contract["baseline_head"]:
            failures.append("report-only run changed HEAD")
        current_branch = git_output(root, "branch", "--show-current")
        if current_branch != contract["baseline_branch"]:
            failures.append("report-only run changed branch")
        current = git_output(root, "status", "--porcelain=v1").splitlines()
        if current != contract["baseline_status"]:
            failures.append("report-only run changed the worktree")
    preflight = contract.get("preflight")
    if preflight is None:
        failures.append("repository preflight has not been recorded")
    elif all_completed:
        failures.extend(
            f"preflight failed: {failure}" for failure in preflight.get("failures", [])
        )
    return failures
