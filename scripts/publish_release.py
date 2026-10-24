"""Publish the built installer as GitHub release v<version> on the public releases repository.

usage: GITHUB_TOKEN=... python scripts/publish_release.py [--repo owner/name] [--notes notes.md] [--draft]
Uploads dist/installer/PhonePilot-<version>-setup.exe and its .sha256 (scripts/build_release.py makes both).
The token is read from the environment only and never written anywhere. A fine-grained token with
"Contents: read and write" on the releases repository alone is enough.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from phonepilot import __version__  # noqa: E402
from phonepilot.config import UpdateConfig  # noqa: E402

API = "https://api.github.com"


def publish(repo: str, version: str, files: list[Path], notes: str, token: str, *, draft: bool = False,
            client: httpx.Client | None = None) -> str:
    """Create release v<version> and upload `files`; returns the release page URL. Refuses an existing tag."""
    own = client is None
    client = client or httpx.Client(timeout=600)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tag = f"v{version}"
    try:
        if client.get(f"{API}/repos/{repo}/releases/tags/{tag}", headers=headers).status_code == 200:
            raise SystemExit(f"{repo} 에 {tag} 릴리스가 이미 있습니다. 버전을 올린 뒤 다시 빌드하세요")
        res = client.post(f"{API}/repos/{repo}/releases", headers=headers,
                          json={"tag_name": tag, "name": f"PhonePilot {version}", "body": notes, "draft": draft})
        if res.status_code == 422:
            raise SystemExit(f"릴리스를 만들지 못했습니다(422). 저장소에 커밋이 하나 이상 있어야 합니다: {res.text[:300]}")
        res.raise_for_status()
        release = res.json()
        upload = release["upload_url"].split("{", 1)[0]
        for f in files:
            print(f"  업로드: {f.name} ({f.stat().st_size / 1e6:.1f} MB)", flush=True)
            r = client.post(upload, params={"name": f.name}, content=f.read_bytes(),
                            headers={**headers, "Content-Type": "application/octet-stream"})
            r.raise_for_status()
        return str(release["html_url"])
    finally:
        if own:
            client.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=UpdateConfig().repo)
    ap.add_argument("--notes", help="Markdown file with the release notes")
    ap.add_argument("--draft", action="store_true")
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN 환경 변수가 필요합니다 (릴리스 저장소에 Contents 쓰기 권한)")
    setup = ROOT / "dist" / "installer" / f"PhonePilot-{__version__}-setup.exe"
    sha = setup.with_name(setup.name + ".sha256")
    for f in (setup, sha):
        if not f.exists():
            raise SystemExit(f"{f} 가 없습니다. 먼저 python scripts/build_release.py 를 실행하세요")
    notes = Path(args.notes).read_text(encoding="utf-8") if args.notes else f"PhonePilot {__version__}"
    url = publish(args.repo, __version__, [setup, sha], notes, token, draft=args.draft)
    print(f"완료: {url}")


if __name__ == "__main__":
    main()
