# 릴리스와 업데이트

PhonePilot은 설치 파일(Inno Setup)로 배포하고, 앱이 GitHub 릴리스를 확인해 스스로 업데이트합니다.
코드 저장소(`ghlee0786/phonepilot`)가 공개 저장소이므로 릴리스도 같은 저장소에 올립니다. 빌드와 업로드는 GitHub Actions가 합니다.

| 항목 | 값 |
| --- | --- |
| 릴리스 저장소 | `config.yaml`의 `update.repo` (기본 `ghlee0786/phonepilot`) |
| 태그 | `v<버전>` (예: `v0.2.0`) — `src/phonepilot/__init__.py`의 `__version__`과 같아야 합니다 |
| 릴리스 첨부 파일 | `PhonePilot-<버전>-setup.exe`, `PhonePilot-<버전>-setup.exe.sha256` |
| 이슈(문제 신고) | 같은 저장소의 Issues |

저장소 이름을 바꾸면 세 곳을 함께 바꿉니다: `src/phonepilot/config.py`(`UpdateConfig.repo`),
`installer/phonepilot.iss`(`ReleasesUrl`), 이 문서.

## CI/CD (GitHub Actions)

| 워크플로 | 언제 | 하는 일 |
| --- | --- | --- |
| [`ci.yml`](../.github/workflows/ci.yml) | `main` push, PR, 수동 | Windows + Python 3.12에서 단위·통합·GUI·시나리오 테스트(화면 없는 offscreen 모드), 스킬 YAML 검증, `docs/MCP_TOOLS.md` 최신 여부, 개인 데이터(`.env`, 제스처 프로필, 녹화 등)가 저장소에 들어오지 않았는지 검사 |
| [`release.yml`](../.github/workflows/release.yml) | `v*` 태그 push, 수동(태그 지정) | 태그와 앱 버전 일치 확인 → 테스트 → vendor 받기 → PyInstaller → 설치 마법사 이미지 → Inno Setup → SHA-256 → GitHub 릴리스 생성·첨부(같은 태그가 있으면 파일만 교체) |

실기기 테스트(`pytest -m device tests\device`)는 폰이 필요해서 CI에서 돌지 않습니다. 릴리스 전에 개발 PC에서 직접 돌립니다.
토큰은 워크플로가 자동으로 받는 `GITHUB_TOKEN`만 씁니다. 따로 만들 비밀값은 없습니다.

## 처음 한 번
1. 저장소 Settings → Actions → General에서 Actions가 켜져 있는지 확인합니다.
2. Issues를 켜 두고 라벨 `bug`를 만들어 둡니다(문제 신고가 이 라벨로 열립니다).

## 릴리스할 때
```powershell
# 1) 버전 올리기: src/phonepilot/__init__.py 의 __version__, pyproject.toml 의 version
# 2) 실기기 테스트 (폰 연결, 잠금 해제)
.venv\Scripts\python.exe -m pytest -m device tests\device
# 3) 커밋 후 태그를 올리면 Actions가 빌드하고 릴리스를 만듭니다
git tag v0.2.0
git push origin main v0.2.0
```

로컬에서 직접 만들 때(오프라인 등):
```powershell
.venv\Scripts\python.exe -m uv pip install -e ".[dev,build]"   # 처음 한 번
.venv\Scripts\python.exe scripts\build_release.py
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
