"""Run-contract persistence and deterministic completion validation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any
from typing import Callable
from typing import Protocol
from typing import TextIO
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA_VERSION = 1
TERMINAL_ACTIONS = (
    "report-only",
    "safe-output-delivery",
    "fix-and-push",
    "full-remediation",
)
DELIVERABLE_STATUSES = ("pending", "completed", "blocked")
DELEGATE_STATUSES = ("pending", "findings", "no-findings", "blocked")
REQUIRED_EVIDENCE = {
    "report-only": ("coverage",),
    "safe-output-delivery": ("validation",),
    "fix-and-push": ("validation", "push"),
    "full-remediation": ("validation", "push", "review-threads", "ci"),
}
IGNORED_DIGEST_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".wrangler",
    "DerivedData",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


class ContractError(RuntimeError):
    """Raised when a run contract is missing or invalid."""


class DigestWriter(Protocol):
    def update(self, value: bytes) -> None: ...


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
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ContractError(
            f"Unable to inspect repository path {start}: {error}"
        ) from error
    if result.returncode:
        raise ContractError(f"Not inside a Git repository: {start}")
    return Path(result.stdout.strip()).resolve()


def state_path(root: Path) -> Path:
    relative = git_output(root, "rev-parse", "--git-path", "agent-guard/contract.json")
    path = Path(relative)
    return path if path.is_absolute() else root / path


def aborted_path(root: Path) -> Path:
    return state_path(root).with_name("aborted.json")


def lock_path(root: Path) -> Path:
    return state_path(root).with_name("contract.lock")


@contextmanager
def contract_lock(root: Path):
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle)
        try:
            yield
        finally:
            release_file_lock(handle)


def acquire_file_lock(handle: TextIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        if not handle.read(1):
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def release_file_lock(handle: TextIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix="contract-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(contract, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def update_contract(
    root: Path, updater: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    with contract_lock(root):
        contract = read_contract(root)
        updater(contract)
        write_contract(root, contract)
        return contract


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
    archive = archive_contract(root, contract)
    marker = aborted_path(root)
    marker.write_text(
        json.dumps(
            {
                "run_id": contract["run_id"],
                "aborted_at": contract["aborted_at"],
                "reason": contract["abort_reason"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return archive


def worktree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    update_digest_field(
        digest,
        git_output(root, "ls-files", "--stage", "-z").encode("utf-8"),
    )
    tracked = set(filter(None, git_output(root, "ls-files", "-z").split("\0")))
    untracked = set(
        filter(
            None,
            git_output(
                root,
                "ls-files",
                "--others",
                "-z",
            ).split("\0"),
        )
    )
    for name in sorted(tracked):
        hash_path_entry(digest, root, root / name)
    for name in sorted(untracked):
        relative = Path(name)
        if any(part in IGNORED_DIGEST_DIRECTORIES for part in relative.parts):
            continue
        hash_path_entry(digest, root, root / relative)
    return digest.hexdigest()


def hash_path_entry(digest: DigestWriter, root: Path, path: Path) -> None:
    if not os.path.lexists(path):
        update_digest_field(digest, path.relative_to(root).as_posix().encode())
        update_digest_field(digest, b"missing")
        return
    metadata = path.lstat()
    update_digest_field(digest, path.relative_to(root).as_posix().encode())
    update_digest_field(digest, f"{metadata.st_mode:o}".encode())
    if stat.S_ISLNK(metadata.st_mode):
        update_digest_field(digest, b"symlink")
        update_digest_field(digest, os.readlink(path).encode())
    elif stat.S_ISREG(metadata.st_mode):
        update_digest_field(digest, b"file")
        content_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                content_digest.update(chunk)
        update_digest_field(digest, content_digest.digest())
    elif stat.S_ISDIR(metadata.st_mode):
        update_digest_field(digest, b"directory")
    else:
        update_digest_field(digest, b"special")
        update_digest_field(digest, str(metadata.st_rdev).encode())


def update_digest_field(digest: DigestWriter, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, byteorder="big"))
    digest.update(value)


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
    expected_evidence = list(REQUIRED_EVIDENCE[contract["terminal_action"]])
    if contract.get("required_evidence") != expected_evidence:
        raise ContractError("Contract required evidence does not match terminal action")
    validate_items(contract["deliverables"], DELIVERABLE_STATUSES, "deliverable")
    validate_items(contract["delegates"], DELEGATE_STATUSES, "delegate")
    for evidence in contract["evidence"]:
        if not isinstance(evidence, dict):
            raise ContractError("Contract evidence entries must be objects")
        if not isinstance(evidence.get("type"), str) or not evidence["type"].strip():
            raise ContractError("Contract evidence type must be non-empty text")
        if not isinstance(evidence.get("value"), str) or not evidence["value"].strip():
            raise ContractError("Contract evidence value must be non-empty text")
        if evidence.get("status", "informational") not in {
            "passed",
            "failed",
            "informational",
        }:
            raise ContractError("Invalid contract evidence status")
    preflight = contract.get("preflight")
    if preflight is not None:
        if not isinstance(preflight, dict) or not isinstance(
            preflight.get("failures"), list
        ):
            raise ContractError("Contract preflight must contain a failures list")
        if not isinstance(preflight.get("head"), str) or not preflight["head"]:
            raise ContractError("Contract preflight must contain the checked HEAD")
        if "branch" in preflight and not isinstance(preflight.get("branch"), str):
            raise ContractError("Contract preflight branch must be text")
        if (
            not isinstance(preflight.get("checked_at"), str)
            or not preflight["checked_at"]
        ):
            raise ContractError("Contract preflight must contain its check time")
        if not isinstance(preflight.get("paths"), list) or not all(
            isinstance(item, str) for item in preflight["paths"]
        ):
            raise ContractError("Contract preflight paths must be text")
        if not all(isinstance(item, str) for item in preflight["failures"]):
            raise ContractError("Contract preflight failures must be text")


def validate_items(
    items: list[Any], allowed_statuses: tuple[str, ...], kind: str
) -> None:
    identifiers: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ContractError(f"Contract {kind} entries must be objects")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ContractError(f"Contract {kind} id must be non-empty text")
        if identifier in identifiers:
            raise ContractError(f"Duplicate {kind} id in contract: {identifier}")
        identifiers.add(identifier)
        if item.get("status") not in allowed_statuses:
            raise ContractError(f"Invalid {kind} status in contract")
        detail_key = "description" if kind == "deliverable" else "scope"
        detail = item.get(detail_key)
        if not isinstance(detail, str) or not detail.strip():
            raise ContractError(f"Contract {kind} {detail_key} must be non-empty text")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not all(
            isinstance(value, str) and value.strip() for value in evidence
        ):
            raise ContractError(f"Contract {kind} evidence must be a text list")


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
    baseline_status = git_output(root, "status", "--porcelain=v1").splitlines()
    if terminal_action != "report-only" and baseline_status:
        raise ContractError("Mutable contracts require a clean worktree")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid4()),
        "created_at": now_iso(),
        "repository_root": str(root),
        "baseline_head": git_output(root, "rev-parse", "HEAD"),
        "baseline_branch": git_output(root, "branch", "--show-current"),
        "baseline_status": baseline_status,
        "baseline_worktree_digest": (
            worktree_digest(root) if terminal_action == "report-only" else None
        ),
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
    normalized_id = delegate_id.strip()
    normalized_scope = scope.strip()
    if not normalized_id or not normalized_scope:
        raise ContractError("Delegate id and scope are required")
    if any(item.get("id") == normalized_id for item in contract["delegates"]):
        raise ContractError(f"Duplicate delegate id: {normalized_id}")
    contract["delegates"].append(
        {
            "id": normalized_id,
            "scope": normalized_scope,
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


def record_evidence(
    contract: dict[str, Any],
    evidence_type: str,
    value: str,
    root: Path | None = None,
    status: str | None = None,
) -> None:
    if not evidence_type.strip() or not value.strip():
        raise ContractError("Evidence type and value are required")
    evidence = {
        "type": evidence_type.strip(),
        "value": value.strip(),
        "status": status
        or ("passed" if evidence_type.strip() == "validation" else "informational"),
        "recorded_at": now_iso(),
    }
    if evidence["status"] not in {"passed", "failed", "informational"}:
        raise ContractError("Invalid evidence status")
    if root is not None:
        evidence["head"] = git_output(root, "rev-parse", "HEAD")
        evidence["worktree_digest"] = worktree_digest(root)
    contract["evidence"].append(evidence)


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
    any_completed = any(
        item.get("status") == "completed" for item in contract["deliverables"]
    )
    if any_completed:
        evidence_types = {item.get("type") for item in contract["evidence"]}
        current_head = git_output(root, "rev-parse", "HEAD")
        current_digest = worktree_digest(root)
        for required in contract["required_evidence"]:
            if required not in evidence_types:
                failures.append(f"missing required {required} evidence")
                continue
            latest = next(
                item
                for item in reversed(contract["evidence"])
                if item.get("type") == required
            )
            if required == "validation" and latest.get("status") != "passed":
                failures.append("required validation evidence is not passing")
            if (
                latest.get("head") != current_head
                or latest.get("worktree_digest") != current_digest
            ):
                failures.append(
                    f"required {required} evidence is stale for current repository contents"
                )
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
        if worktree_digest(root) != contract.get("baseline_worktree_digest"):
            failures.append("report-only run changed repository file contents")
    elif contract["terminal_action"] != "safe-output-delivery" and git_output(
        root, "status", "--porcelain=v1"
    ):
        failures.append("mutable run has uncommitted worktree changes")
    preflight = contract.get("preflight")
    if preflight is None:
        failures.append("repository preflight has not been recorded")
    elif any_completed:
        failures.extend(
            f"preflight failed: {failure}" for failure in preflight.get("failures", [])
        )
        current_head = git_output(root, "rev-parse", "HEAD")
        current_branch = git_output(root, "branch", "--show-current")
        if (
            preflight.get("head") != current_head
            or preflight.get("branch") != current_branch
        ):
            failures.append(
                "repository preflight is stale for current checkout "
                f"{current_branch or '(detached)'} at {current_head}"
            )
    return failures


def complete_and_archive(root: Path) -> tuple[list[str], Path | None]:
    with contract_lock(root):
        contract = read_contract(root)
        failures = completion_failures(contract, root)
        if failures:
            return failures, None
        return [], archive_contract(root, contract)
