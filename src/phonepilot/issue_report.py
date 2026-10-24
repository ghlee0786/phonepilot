"""Problem reports as pre-filled GitHub issues, opened in the browser for the user to review and submit.

No token ships with the app: the user's own GitHub login files the issue. The log lines come from the
redacted RING buffer; `scrub` masks what the redactor cannot know about (paths, tokens, e-mails).
"""

from __future__ import annotations

import os
import platform
import re
import sys
import traceback
from collections.abc import Iterable
from urllib.parse import urlencode

from phonepilot import __version__

NEW_ISSUE = "https://github.com/{repo}/issues/new"
MAX_URL = 7500  # GitHub rejects longer issue URLs
LOG_LINES = 40

_SCRUB = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "gh***"),
    (re.compile(r"(?i)(bearer|token|authorization)([\s:=\"']+)[A-Za-z0-9._\-]{12,}"), r"\1\2***"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "***@***"),
]


def scrub(text: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "~":
        text = text.replace(home, "%USERPROFILE%")
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return text


def describe_exception(exc: BaseException) -> tuple[str, str]:
    """(one-line summary, traceback) for an exception, PhonePilotError codes first."""
    code = getattr(exc, "code", "")
    summary = f"{code}: {exc}" if code else f"{type(exc).__name__}: {exc}"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return summary, tb


def build_report(
    summary: str,
    *,
    details: str = "",
    device: str = "",
    connection: str = "",
    log_lines: Iterable[str] = (),
    incidents: Iterable[str] = (),
) -> tuple[str, str]:
    """Issue (title, body) in Korean, scrubbed."""
    install = "설치본" if getattr(sys, "frozen", False) else "소스 실행"
    logs = list(log_lines)[-LOG_LINES:]
    parts = [
        "## 무슨 일이 있었나요?",
        "<!-- 어떤 작업을 하던 중이었는지 적어 주세요 -->",
        "",
        "## 오류",
        f"`{summary}`",
        "",
        "## 환경",
        f"- PhonePilot {__version__} ({install})",
        f"- Windows {platform.release()} ({platform.version()})",
        f"- 기기: {device or '알 수 없음'}",
        f"- 연결 상태: {connection or '알 수 없음'}",
    ]
    recent = list(incidents)
    if recent:
        parts += ["", "## 최근 연결 장애", *[f"- {line}" for line in recent]]
    if details:
        parts += ["", "<details><summary>상세 (Traceback)</summary>", "", "```", details.strip(), "```", "</details>"]
    if logs:
        parts += ["", f"<details><summary>최근 로그 {len(logs)}줄</summary>", "", "```", *logs, "```", "</details>"]
    parts += ["", "> 자동 작성됨. 등록 전에 개인정보가 없는지 확인해 주세요."]
    title = scrub(f"[오류] {summary}")[:120]
    return title, scrub("\n".join(parts))


def issue_url(repo: str, title: str, body: str, labels: str = "bug") -> str:
    """Pre-filled new-issue URL; the body is cut from the end (the logs go first) to stay under GitHub's URL limit."""
    base = NEW_ISSUE.format(repo=repo)
    note = "\n\n…(길어서 잘렸습니다)"
    keep, text = len(body), body
    while True:
        url = base + "?" + urlencode({"title": title, "labels": labels, "body": text})
        if len(url) <= MAX_URL or keep < 200:
            return url
        keep = int(keep * 0.85)
        text = body[:keep].rstrip() + note
