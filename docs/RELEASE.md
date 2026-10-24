# 릴리스와 업데이트

PhonePilot은 설치 파일(Inno Setup)로 배포하고, 앱이 GitHub 릴리스를 확인해 스스로 업데이트합니다.
코드 저장소는 비공개이므로, 설치 파일은 **공개 릴리스 전용 저장소**에 올립니다.

| 항목 | 값 |
| --- | --- |
| 릴리스 저장소 | `config.yaml`의 `update.repo` (기본 `ghlee0786/phonepilot-releases`) |
| 태그 | `v<버전>` (예: `v0.2.0`) |
| 릴리스 첨부 파일 | `PhonePilot-<버전>-setup.exe`, `PhonePilot-<버전>-setup.exe.sha256` |
| 이슈(문제 신고) | 같은 릴리스 저장소의 Issues |

릴리스 저장소 이름을 바꾸면 세 곳을 함께 바꿉니다: `src/phonepilot/config.py`(`UpdateConfig.repo`),
`installer/phonepilot.iss`(`ReleasesUrl`), 이 문서.

## 처음 한 번
1. GitHub에 **공개** 저장소를 만듭니다(예: `phonepilot-releases`). README 하나라도 커밋해 두어야 릴리스를 만들 수 있습니다.
2. Issues를 켜 두고, 라벨 `bug`를 만들어 둡니다(문제 신고가 이 라벨로 열립니다).
3. 세분화 토큰(fine-grained PAT)을 만듭니다: 대상 저장소는 릴리스 저장소 하나, 권한은 **Contents: Read and write**만.

## 릴리스할 때
```powershell
# 1) 버전 올리기: src/phonepilot/__init__.py 의 __version__, pyproject.toml 의 version
# 2) 빌드: 실행 파일 → 설치 마법사 이미지 → 인스톨러 → SHA-256
.venv\Scripts\python.exe -m uv pip install -e ".[dev,build]"   # 처음 한 번
.venv\Scripts\python.exe scripts\build_release.py
# 3) 올리기 (토큰은 환경 변수로만 전달하고 저장하지 않습니다)
$env:GITHUB_TOKEN = "<토큰>"; .venv\Scripts\python.exe scripts\publish_release.py --notes notes.md
Remove-Item Env:GITHUB_TOKEN
```

`build_release.py` 옵션: `--exe-only`(실행 파일까지만), `--skip-exe`(기존 dist 재사용), `--skip-assets`, `--iscc <ISCC.exe 경로>`.

## 앱 쪽 동작
- **업데이트 확인**: 시작 5초 뒤(하루 1회) 또는 [기기] 탭 → 앱 정보 → [업데이트 확인].
  새 버전이면 변경 내용을 보여 주고, [설치]를 누르면 내려받아 **SHA-256을 확인한 뒤에만** 설치 파일을 조용히 실행합니다.
  `.sha256`이 없거나 다르면 설치하지 않습니다. 소스에서 실행 중이면 릴리스 페이지만 엽니다.
- **문제 신고**: 예상하지 못한 오류가 나거나 [문제 신고]를 누르면, 버전·Windows·기기·연결 상태·오류와 최근 로그 40줄을 채운
  이슈 작성 페이지를 브라우저에서 엽니다. 사용자가 확인하고 직접 등록합니다(앱에 토큰 없음).
  로그는 비밀값이 가려진 상태이고, 사용자 폴더 경로·토큰 형태 문자열·이메일을 한 번 더 가립니다.

## 설치 프로그램
- 관리자 권한 없이 `%LOCALAPPDATA%\Programs\PhonePilot`에 설치합니다(업데이트 때 UAC 창 없음).
- 첫 설치 마법사: 기능 소개 4장 → 설치 위치 → 바로가기 → Claude Desktop 연결 → Anthropic API 키(선택) → 폰 준비 안내.
- 업데이트(이미 설치됨)나 조용한 설치에서는 소개·설정 화면을 건너뛰고, Claude 설정과 `.env`를 건드리지 않습니다.
- 제거해도 사용자 설정(`%APPDATA%\PhonePilot`)은 남깁니다.
