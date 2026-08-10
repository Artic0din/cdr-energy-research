#!/usr/bin/env python3
"""Validate host-neutral pull-request metadata from a GitHub event payload."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


CONVENTIONAL_TITLE = re.compile(
    r"^(?:feat|fix|test|refactor|perf|docs|style|chore|ci|build|revert)"
    r"(?:\([a-z0-9][a-z0-9._/-]*\))?: .+"
)
ISSUE_BRANCH = re.compile(
    r"^(?:(?:feat|fix|test|refactor|perf|cursor)/"
    r"|repo-assist/(?:issue-)?)"
    r"(\d+)(?:-|$)",
    re.IGNORECASE,
)
ISSUE_LINK = re.compile(r"(?im)^\s*(?:[-*+]\s*)?(?:fixes|closes|resolves)\s+#(\d+)\b")
VALIDATION_SECTION = re.compile(
    r"(?ims)^#{1,3}\s+(?:test plan|tests?|validation|verification)\s*$"
    r"(?P<content>.*?)(?=^#{1,3}\s+|\Z)"
)
NEGATIVE_VALIDATION = re.compile(
    r"(?i)^(?:n/?a|none|not applicable|not required|not run|not tested|pending|"
    r"skipped|todo)(?:\b|[.!])"
)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def validate_pull_request(pull_request: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    title = str(pull_request.get("title") or "")
    body = HTML_COMMENT.sub("", str(pull_request.get("body") or ""))
    branch = str(pull_request.get("head", {}).get("ref") or "")
    if pull_request.get("draft"):
        failures.append("pull request must be ready for review, not draft")
    if not CONVENTIONAL_TITLE.fullmatch(title):
        failures.append("pull-request title is not conventional")
    if not body.strip():
        failures.append("pull-request body is empty")
    elif not (validation_match := VALIDATION_SECTION.search(body)):
        failures.append("pull-request body lacks a test or validation section")
    elif not meaningful_validation(validation_match.group("content")):
        failures.append("pull-request validation section has no verification evidence")
    issue_match = ISSUE_BRANCH.search(branch)
    if issue_match:
        linked = {match.group(1) for match in ISSUE_LINK.finditer(body)}
        if issue_match.group(1) not in linked:
            failures.append(
                f"issue-backed branch {branch} must link Fixes #{issue_match.group(1)}"
            )
    return failures


def meaningful_validation(content: str) -> bool:
    for line in content.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        if re.match(r"^\[\s\]", stripped):
            continue
        stripped = re.sub(r"^\[[ xX]\]\s*", "", stripped)
        if not stripped:
            continue
        if ISSUE_LINK.fullmatch(stripped):
            continue
        if NEGATIVE_VALIDATION.match(stripped):
            continue
        return True
    return False


def render(failures: list[str]) -> str:
    if not failures:
        return "PASS: pull-request policy is satisfied.\n"
    return "FAIL:\n" + "\n".join(f"- {failure}" for failure in failures) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.event.read_text(encoding="utf-8"))
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        parser.error("event payload does not contain a pull_request object")
    failures = validate_pull_request(pull_request)
    print(render(failures), end="")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
