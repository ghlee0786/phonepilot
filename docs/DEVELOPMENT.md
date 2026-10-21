# PhonePilot 개발 문서

코드를 고치거나 기능을 더할 때 읽는 문서. AI가 폰을 조작하는 방법은 [MCP_GUIDE.md](MCP_GUIDE.md), 요구사항 원본은 [PRD.md](PRD.md).
PRD 조항 번호(FR-xxx 기능 요구, SEC-xxx 보안, TC-xxx 테스트 케이스)는 코드 docstring과 테스트 이름에 그대로 쓰인다 — `grep FR-INP-035`로 구현 위치를 찾을 수 있다.

---

## 1. 개발 환경

| 항목 | 값 |
| --- | --- |
| Python | `.venv\Scripts\python.exe` (uv 관리 CPython 3.12.15). 시스템 Python 3.12.0b4는 베타라 사용 금지 |
| 설치 | `python -m uv pip install --python .venv\Scripts\python.exe -e ".[dev]"` |
| 주요 의존성 | PySide6(GUI), av(PyAV/FFmpeg 디코드), adbutils, mcp>=2.3(MCP SDK 2.x), anthropic(1.x), uvicorn, pydantic, PyYAML, Pillow, numpy, qrcode, tzdata |
| 기기 바이너리 | `scripts/fetch_vendor.py` → `vendor/platform-tools/adb.exe`, `vendor/scrcpy-server`(v4.1, SHA-256 `deacb991…0cae` 고정). git 제외 |
| 실행 파일 | `.venv\Scripts\phonepilot.exe`(= `python -m phonepilot`), `.venv\Scripts\phonepilot-mcp.exe`(stdio 중계기) |
| 사용자 파일 | `%APPDATA%\PhonePilot\`(.env, secrets.meta.yaml, config.yaml, skills\, approved_skills.json, gesture_profiles.yaml), `%LOCALAPPDATA%\PhonePilot\`(mcp.token, 일일 카운터, order_ledger.json — 합계·날짜만), `%TEMP%\PhonePilot\`(세션 임시, 종료 시 삭제). 무선 페어링 신뢰 정보는 adb 자체 키(`%USERPROFILE%\.android`)에 있다 |
| 산출물 폴더 | `%USERPROFILE%\Documents\PhonePilot-Recovery`(백업 코드), `%USERPROFILE%\Documents\PhonePilot-Assets`(push 허용 폴더) |

`pyproject.toml`: `pythonpath=["."]`, `asyncio_mode=auto`, 마커 `device`, 기본 `addopts = -m 'not device'`. 파일에 UTF-8 BOM을 넣지 말 것(파싱 실패 전력 있음).

## 2. 전체 구조와 스레드 모델

```
 Qt GUI 스레드 (gui/*)                     CoreRuntime 스레드 (asyncio 루프, runtime.py)          디코더 스레드 (core/decoder.py)
 ┌──────────────────────────┐  core_call  ┌───────────────────────────────────────────┐  packets  ┌────────────────────┐
 │ MainWindow, MirrorView,  │────────────▶│ RealDevice/FakeDevice (DeviceBackend)     │──────────▶│ PyAV H.264 디코드   │
 │ panels, dialogs          │◀────────────│ InputArbiter, Observer, ToolService       │           │ (D3D11VA, SLICE,    │
 │ Bridge: 콜백→Qt 시그널    │  signals    │ PolicyEngine, ApprovalCenter, Vault       │           │  LOW_DELAY)         │
 │ InputPump: 마우스/키 →    │             │ WorkflowEngine, BuiltinAgent              │           └─────────┬──────────┘
 │   arbiter.human_*         │             │ MCP 서버 (uvicorn, 127.0.0.1:47800)        │                     │ Frame
 └──────────▲───────────────┘             └───────────────────────────────────────────┘             ┌───────▼────────┐
            └──────────────────────── MirrorView가 최신 프레임만 그림 ◀───────────────────────────────│ FrameBus       │
                                                                                                      │ (최신 프레임만) │
                                                                                                      └────────────────┘
```

- **CoreRuntime**(`runtime.py`)이 백그라운드 스레드에서 asyncio 루프를 돌리고 기기·도구·워크플로·MCP 서버를 소유한다. GUI는 `runtime.call(coro)`(Future) /
  `call_sync`로 부르고, `runtime.emit(event, data)` → `Bridge`가 Qt 시그널로 바꿔 GUI 스레드로 전달한다. Qt 객체를 코어 스레드에서 만지지 말 것.
- **FrameBus**: "최신 프레임이 이긴다". 렌더러·Observer·녹화가 공유. 밀린 프레임은 버린다(지연 최소화).
- **InputArbiter**(`core/arbiter.py`): 사람·AI 입력의 유일한 통로. 사람 입력이 항상 우선 — 실행 중 AI 제스처를 취소하고 AI를 일시정지한다.
  `HUMAN_GRACE_S=1.5`, `LEASE_TTL_S=60`. 긴급 정지(kill) 시 열린 포인터를 모두 UP으로 닫는다.
- **ToolService**(`agent/tools.py`): MCP 서버·내장 에이전트·워크플로 엔진이 공유하는 도구 구현. 행동은 전부 `_act`를 지난다:
  rate limit → `check_ai_allowed` → lease → `resolve_target` → `_gate`(정책 분류 → 승인 → 승인 후 화면·대상 재검증) → 실행 → 안정화 대기 → 결과 + 타임라인.

## 3. 모듈 지도

### core/ — 기기·영상·입력
| 파일 | 역할 |
| --- | --- |
| `scrcpy/protocol.py` | scrcpy 와이어 프로토콜: 제어 메시지(`inject_touch` 32B, `inject_keycode` 14B, `inject_text` ≤300B, `inject_scroll` 21B, 클립보드, 디스플레이 전원), 기기 메시지 파서, 스트림 헤더(`parse_stream_header` → `SessionMeta`/`PacketHeader`), `server_args()`, FakeDevice용 `decode_control` |
| `scrcpy/server.py` | `ScrcpyServer`: jar push(해시 비교, 서버가 시작 시 jar를 지우므로 매번 다시 push됨) → `adb forward tcp:27183~ → localabstract:scrcpy_<scid>` → `app_process` 실행(로그 스레드) → `connect()`(영상 소켓: 더미 바이트 대기, 이어서 제어 소켓) → `pump()` |
| `real_device.py` | `RealDevice(DeviceBackend)`: 위를 묶고 디코더·FrameBus 연결, `dump_ui`(uiautomator dump → /sdcard 임시 파일 즉시 삭제), `foreground`(dumpsys activity), `shell`, `push`. `VideoSettings` |
| `adb.py` | `DeviceManager`(번들 adb 서버, 기기 목록), `collect_profile`(모델·Android·해상도·밀도·IME·내비 방식; 기기 키는 `ro.serialno`), `preflight` |
| `wireless.py` | 무선 디버깅: QR(`WIFI:T:ADB;S:<service>;P:<pw>;;`) 페어링 → mDNS `_adb-tls-pairing._tcp` 발견 → `adb pair`; 코드 페어링; `_adb-tls-connect._tcp`로 재연결. `validate_host_port`는 사설 IP만 허용(공인 IP 차단) |
| `decoder.py` | `VideoDecoder` 스레드: H.264 HW 디코드(D3D11VA, 실패 시 SW), SLICE 스레딩 + LOW_DELAY(프레임 스레딩은 여러 프레임을 붙잡아 지연↑) |
| `framebus.py` | 최신 프레임 보관 + 구독 |
| `gesture.py` | 결정론적 제스처 합성: `tap/double_tap/long_press/swipe/precise_scroll/fling/drag/pinch` → `TouchStep` 목록. cubic-bezier 이징(제어점 파라미터화). 무작위 지터 없음(의도) |
| `calibration.py` | 제스처 프로파일(FR-INP-035~039): 사용자가 미러 창에서 한 탭·길게 누르기·스와이프를 `CalibrationRecorder`가 기록 → `measure()` → `build_profile()`이 중앙값·클램프로 `GestureProfile` 생성 → `ProfileStore`(`gesture_profiles.yaml`, 기기 키별). 정착 시간은 "최종 위치 1px 이내 도착" 기준 |
| `coords.py` | 창↔프레임↔기기 좌표, 비율 유지 레터박스(`fit_rect`), 0~1000 정규화 |
| `arbiter.py` | 위 2장 |
| `device.py` | `DeviceBackend` ABC: `send_control`, `dump_ui`, `shell`, `foreground`, `push`, `display_size`, `frame_size`, `framebus`, 좌표 변환 |

### observe/ — 화면 이해
| 파일 | 역할 |
| --- | --- |
| `uitree.py` | uiautomator XML → `UIElement`(역할 추론, 라벨, stable_key, 비밀번호 필드 마스킹) → CSV 직렬화, `find`/`match_selector`(text/text_any/text_contains/text_regex/desc/desc_contains/resource_id/role/index/near/within) |
| `vision.py` | dhash + 평균 해시(평탄한 색 변화도 잡기 위해 추가), `StabilityTracker`, SoM 오버레이, 마스킹, 인코딩, 크롭 |
| `observer.py` | `Observation` 생성(트리 + 프레임 병렬), obs_id 보관, 민감 앱/화면 판정과 마스킹, `wait_stable` |

### security/ — 비밀값·정책·승인
| 파일 | 역할 |
| --- | --- |
| `secrets.py` | `Secret`(bytearray, `repr/str/json/pickle` 거부, `reveal()` 컨텍스트로만 접근, `zero()`), `SecretVault`(.env + secrets.meta.yaml, `refs()`는 이름·종류만) |
| `redactor.py` | 모든 출력 마스킹: 비밀값 원문·부분, 카드번호(Luhn), base32 OTP 키 패턴 등 |
| `policy.py` | `PolicyEngine.classify(ActionContext)` → ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK. 키워드 체(`DEFAULT_KEYWORDS` + `policy/keywords.yaml`), 민감 앱, 금액 정규식, 참여 행동은 워크플로/배치 안에서 BLOCK, 2단계 인증 문맥의 전화번호 추가 BLOCK |
| `approvals.py` | `ApprovalCenter`: GUI 핸들러 설치(없으면 전부 거부), 대기 중 요청은 kill 시 취소, "모두 중지"는 kill 유발. `HumanRequest` |
| `limits.py` | `RateLimiter`, `DailyCounters`(날짜·합계만 저장) |
| `vault.py` | `RecoveryVault`: 백업 코드 저장(유일하게 비밀을 디스크에 쓰는 곳, 사용자 요청에 의해). ACL 제한, 선택적 DPAPI |
| `cleanup.py` | `SessionCleanup`: 종료 시 비밀 메모리 0으로, 기기 클립보드 비우기, 임시 파일·서버 jar 삭제, `%TEMP%\PhonePilot` 정리 |

### agent/ — MCP·에이전트
| 파일 | 역할 |
| --- | --- |
| `tools.py` | `ToolService`(48개 코어 도구) |
| `integration_tools.py` | `IntegrationMixin`: YouTube 조사, 시세·잔고, 주문 초안/제출/취소(`integrations/orders.OrderGate` 경유) |
| `mcp_server.py` | `build_server(service)`가 `MCPServer`(SDK 2.x)에 도구 등록, `to_result`/`to_error`(Redactor 적용, 200KB 상한), `AuthMiddleware`(Bearer + loopback Origin, `hmac.compare_digest`), `load_or_create_token`, `http_app` |
| `stdio_shim.py` | `phonepilot-mcp`: stdio ↔ HTTP 중계. `_Upstream`이 한 태스크에서 연결을 소유(anyio 스코프 규칙), 필요할 때 토큰 파일을 다시 읽어 재연결, 실패 호출은 재시도하지 않음 |
| `builtin_agent.py` | 내장 Claude 루프(수동 tool-use 루프, `claude-opus-5-5`, 히스토리 append-only, 비용·단계 예산) |
| `prompts.py` | 운영 프롬프트(MCP prompts와 내장 에이전트 공용) |
| `timeline.py` | 메모리 전용 타임라인(마스킹됨, 사용자가 내보낼 때만 파일) |

### skills/ — 실행 스킬
`schema.py`(Pydantic: Skill/Step/InputSpec/Requires/OnError, 단계 종류 11개), `loader.py`(`SkillRegistry`, SEC-SKL-01~06: 비밀값 템플릿 금지, 참여 반복 금지,
사용자 스킬은 해시 승인 필요), `engine.py`(`WorkflowEngine` 상태 기계, 실행 상태는 메모리만, 단계별 `expect` 사후조건, `on_fail`).
`secret_input` 단계의 ref는 입력값 이름(`inputs.alias`)으로 렌더링되며 비밀값 자체는 템플릿에 들어가지 않는다.

### gui/
`app.py`(QApplication), `main_window.py`(툴바·상태 배지·단축키·승인 컨트롤러), `mirror_view.py`(비율 유지 표시, 마우스/키 → InputPump, F11 전체화면),
`panels.py`(타임라인·워크플로·승인·기기 탭; 기기 탭은 QScrollArea 안에 무선 연결·제스처 보정 섹션), `dialogs.py`(승인 창(비모달), 사람 개입 배너, 스킬 입력, 무선 QR/코드),
`bridge.py`, `hotkeys.py`(전역 Pause / Ctrl+Alt+End = 긴급 정지, RegisterHotKey).

### integrations/
`upbit.py`(요청마다 HS256 JWT, query_hash SHA-512), `kis.py`(한국투자증권, 기본 모의투자), `orders.py`(`OrderGate`: 초안 → 한도·허용 종목 → 강화 승인 → 제출, 상태 불명 시 재제출 금지), `youtube.py`(Data API v3 읽기 전용).

### testing/
`fake_device.py`(`FakeDevice`: UI 트리 상태 기계 + 한글 폰트로 그린 프레임, `demo_screens()`), `harness.py`(`build_stack(device)` → 전체 코어 조립).

## 4. scrcpy 연결 상세 (실기기 경험 포함)

연결 순서(`RealDevice.connect`):
1. `ScrcpyServer.push()` — 원격 sha256이 같으면 생략. **서버는 시작하면서 자기 jar를 지운다** → 다음 연결 땐 다시 push됨.
2. `forward()` — `tcp:27183`부터 빈 포트.
3. `start()` — `CLASSPATH=/data/local/tmp/phonepilot-server.jar app_process / com.genymobile.scrcpy.Server 4.1 scid=… tunnel_forward=true audio=false …`
4. `connect()` — adb는 서버가 listen하기 전에도 TCP를 받아 주고 바로 닫는다(EOF) → 재시도. **한 번 받아들여진 소켓이 곧 영상 소켓**이므로 그 소켓에서 더미 바이트를
   기다려야 한다(다시 연결하면 서버가 그것을 제어 소켓으로 받아 버림 — 실제로 겪은 버그). 이어서 제어 소켓 연결 → 기기 이름(64B) + 코덱 id(4B).
5. `pump()` — 12바이트 헤더: bit63 = 세션 패킷(해상도), bit62 = config(SPS/PPS), bit61 = 키프레임, 나머지 PTS. 이어서 payload.

**Galaxy S21+ / Android 15 크래시 (중요)**: scrcpy-server 5.0.1에서 `stack corruption detected (-fstack-protector)`로 `app_process`가 죽었다.
이분 탐색 결과 원인은 **서버 인자 개수**(14개 이상이면 크래시, 버전 무관). 조치: 서버를 4.1로 고정(스트림·제어 프로토콜이 5.0.1과 바이트 단위로 동일)하고
`server_args()`가 **기본값이 아닌 옵션만** 넘기게 바꿈(기본 7개). `tests/unit/test_protocol.py::test_server_args_contains_version_and_scid`가 개수 상한을 지킨다.
옵션을 추가할 땐 기본값과 다를 때만 넣고, 13개를 넘기지 말 것. 결과: 5/5 성공, 첫 프레임 ~1초, 864×1920(max_size 1920 기준 스케일) HW 디코드.

진단 팁: 서버 로그는 `ScrcpyServer.server_log`(최근 200줄)와 앱 로그에 `server:` 접두어로 남는다. 직접 실험할 때 Git Bash에서는
`MSYS_NO_PATHCONV=1 vendor/platform-tools/adb.exe shell ...`(안 붙이면 `/data/...`가 `C:/Program Files/Git/data/...`로 바뀜).

서버 버전을 올릴 때: `scripts/fetch_vendor.py`의 `SCRCPY_VERSION`/`SCRCPY_SERVER_SHA256`, `protocol.SCRCPY_SERVER_VERSION`을 함께 바꾸고,
해당 버전 Java 소스(`Options.java`, `Streamer.java`, `ControlMessageReader.java`)와 헤더 레이아웃을 대조한 뒤 실기기 테스트.

## 5. 테스트

```powershell
.venv\Scripts\python.exe -m pytest                       # 249개, ~40초, 기기 불필요
.venv\Scripts\python.exe -m pytest tests\unit -q          # 단위만
.venv\Scripts\python.exe -m pytest -m device tests\device # 실기기 (USB 연결 + 잠금 해제)
```

| 폴더 | 내용 |
| --- | --- |
| `tests/unit` | 프로토콜 바이트 레이아웃(TC-MIR-01/02), 좌표·제스처, 관찰, 보안(카나리 비밀값 누출 검사, 정책 분류), 스킬 검증, 캘리브레이션, 무선, 연동 |
| `tests/integration` | ToolService 전체 흐름(FakeDevice), MCP in-process `Client`, stdio 중계기(앱 실행/미실행/나중에 실행), 스트림 파이프라인, 워크플로, 캘리브레이션 흐름, 런타임+에이전트 |
| `tests/gui` | pytest-qt: 창 크기, 전체화면 비율, 승인 창, 패널 |
| `tests/device` | TC-DEV-01~03: 실기기 연결·첫 프레임·제어 왕복 |

- 공용 픽스처 `tests/conftest.py`: 카나리 비밀값(`CANARY_PASSWORD` 등)을 넣은 vault. **어떤 도구 출력·로그·타임라인에도 카나리가 나오면 실패**하는 테스트가 있다.
- MCP 테스트는 SDK 2.x의 `mcp.client.Client(server)`(in-process)를 쓴다. 도구 결과는 `structured_content`, 오류는 `is_error`.
- 새 기능에는 FakeDevice 기반 테스트를 붙인다. 실기기 전용 동작은 `@pytest.mark.device`.

## 6. 확장 방법

### 6.1 MCP 도구 추가
1. `ToolService`에 `async def my_tool(self, actor: str = "agent", ..., **args)` — 폰을 조작하면 반드시 `return await self._act("my_tool", actor, args, run, ...)`.
2. `agent/mcp_server.py`의 `build_server`에 등록(설명은 AI가 읽는 문장, 주석 `RO`/`ACT`/`DESTRUCTIVE`).
3. 읽기 전용이면 `security/policy.py`의 `READ_ONLY_TOOLS`, 항상 강화 승인이면 `ALWAYS_HIGH_TOOLS`에 추가.
4. 내장 에이전트는 `builtin_agent.tool_definitions`가 서버 정의에서 가져오므로 따로 할 일 없음(확인만).
5. 테스트(단위 + `tests/integration/test_mcp.py`), `scripts/gen_tool_reference.py` 실행해 `docs/MCP_TOOLS.md` 갱신, 필요하면 `GROUPS`에 분류 추가.

### 6.2 실행 스킬(YAML) 추가
`skills/<영역>/<이름>.yaml` → `python -m phonepilot skill validate <파일>` → `tests/unit/test_skills.py`가 저장소 스킬 전체를 로드해 검증한다.
비밀값은 `secret_input` 단계에서 `ref: "GOOGLE_{{inputs.alias}}_PASSWORD"` 형태(키 이름 템플릿)만. 좋아요·구독 반복은 로더가 거부한다.

### 6.3 Claude Code 스킬 추가
`.claude/skills/<이름>/SKILL.md`(frontmatter `name`, `description`). 절차 설명서이며 도구 이름은 MCP 도구명 그대로.

### 6.4 정책 키워드·민감 앱
`policy/keywords.yaml`, `policy/sensitive_apps.yaml`을 고치면 코드 기본값과 합쳐진다. 분류 변화는 `tests/unit/test_security.py`에 케이스 추가.

### 6.5 설정 항목
`config.py`의 Pydantic 모델에 기본값과 함께 추가 → `config.example.yaml` 갱신. 비밀값은 설정에 넣지 않는다.

## 7. 문제 해결

| 증상 | 원인/조치 |
| --- | --- |
| `stack corruption detected` / 서버 즉시 종료 | 서버 인자 13개 초과 또는 jar 없음. 4장 |
| `SERVER_START_TIMEOUT` | 서버 로그 확인(`server:` 로그). 폰에서 다른 scrcpy가 떠 있으면 `adb shell pkill -f com.genymobile.scrcpy.Server` |
| 첫 연결 후 제어가 안 됨 | 영상/제어 소켓 순서 문제(4장 4번) — `connect()` 수정 금지 |
| 화면이 검정 | FLAG_SECURE 앱(은행 등) — 정상 동작, `secure_surface: true` |
| `DEVICE_UNAUTHORIZED` | 폰의 USB 디버깅 허용 팝업 |
| UI 트리가 비어 있음 | 게임·일부 WebView. `observe(image="som")` + 좌표 |
| 한글 입력 안 됨 | `type_text`는 클립보드 붙여넣기 경로 사용(`input.non_ascii_path: clipboard`). 비밀값 한글은 미지원 |
| MCP 도구가 안 보임 | 앱 미실행 상태로 Claude 세션 시작 → 앱 실행 후 `/mcp` 재연결 |
| 401 Unauthorized(HTTP 직결) | 앱 재시작으로 토큰 바뀜 → stdio 중계기 사용 또는 `mcp.token_fixed: true` |
| pyproject 파싱 오류 | UTF-8 BOM 제거 |

## 8. 결정 기록

| 날짜 | 결정 | 이유 |
| --- | --- | --- |
| 2026-10-12 | 계정 '에이징'(유사 채널 자동 스와이프·좋아요) 기능 미구현, "테스트용" 요청도 거절 | 실제 YouTube/계정에서 의도와 무관하게 똑같이 동작 → 허위 참여 정책 위반. FakeDevice 탐색 루프 테스트와 채널 런치 체크리스트·읽기 전용 분석으로 대체 (PRD 3.3) |
| 2026-10-12 | 제스처에 무작위 지터·봇 회피 없음. 대신 사용자 터치로 보정한 결정론적 프로파일 | 사람다운 움직임 요구는 충족하되 탐지 회피 도구가 되지 않게 |
| 2026-10-12 | 구글 2단계 인증은 OTP 앱만, 전화번호 등록은 정책 BLOCK | 사용자 요구 |
| 2026-10-12 | 비밀값은 .env에만, AI에는 키 이름만 | 사용자 요구(흔적 없음 + .env 활용) |
| 2026-10-12 | scrcpy-server 4.1 고정 + 최소 인자 | Samsung Android 15 크래시(4장) |
| 2026-10-12 | 무선 디버깅은 QR 페어링 우선(코드 페어링 보조), 사설 IP만 | 사용자가 "추천 방향" 선택. 공용망 노출 방지 |
| 2026-10-12 | 투자는 공식 API + 승인형 주문만, 출금 도구 없음, KIS 기본 모의 | 금융 앱 화면 자동화·무인 매매 위험 |
| 2026-10-12 | MCP는 HTTP 서버(앱 내부) + stdio 중계기, Claude Code는 중계기 사용 | 앱 재시작마다 토큰이 바뀌어도 세션 유지 |

## 9. 남은 일

- 폰 잠금 해제 상태에서 `pytest -m device tests\device` 실행·기록.
- 실기기에서 무해한 MCP 조작 시연(홈 → 설정 → 스크롤 → 뒤로)과 지연 측정(PRD 목표: 입력→화면 반영 p95).
- 무선 QR 페어링 실기기 검증(같은 Wi-Fi, 개발자 옵션 → 무선 디버깅).
- 연동(YouTube/Upbit/KIS)은 키를 넣고 `integrations.*.enabled: true`로 켠 뒤 실제 호출 검증(현재는 단위 테스트만).
- 배포 패키징(PyInstaller, `paths.repo_root`가 frozen 대응)은 미진행.
