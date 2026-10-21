# PhonePilot MCP 도구 레퍼런스

`scripts/gen_tool_reference.py`가 실제 서버 정의에서 생성했다(2026-10-12, 도구 57개). 직접 고치지 말고 스크립트를 다시 실행한다.

공통 규칙: 결과는 `structuredContent`(JSON) + 같은 내용의 텍스트 + 필요 시 이미지. 오류는 `isError: true`와 `{code, message, hint, retryable}`. 모든 출력은 비밀값·민감 패턴이 마스킹된 뒤 나간다.

행동 도구: `reason`(타임라인·승인 창에 표시)을 받고, 실행 후 화면 안정화까지 자동 대기한다. 결과는 `{ok, screen_changed, package_changed, new_dialogs, warnings[UNSTABLE_SCREEN|LOOP_SUSPECTED], approval?, elapsed_ms}`(`tap`은 `return_observation=true`면 새 관찰 포함). 대상 지정은 `element`+`obs_id`(권장) / `selector` / `x_norm`+`y_norm`(0~1000) 중 하나.

## 관찰 (읽기 전용)

### `observe`

Observe the current screen: foreground app, numbered interactive elements, screen text, dialogs. image: none|som|plain.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `image` | str |  | "none" |
| `max_elements` | int |  | 150 |

### `screenshot`

Screenshot of the phone (masked). region_norm=[x0,y0,x1,y1] in 0..1000 crops.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `max_edge` | int |  | 1280 |
| `format` | str |  | "jpeg" |
| `region_norm` | list[int] | null |  | null |

### `zoom`

Zoom into a screen region (0..1000 normalized bounds) at higher resolution.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `bounds_norm` | list[int] | 예 |  |
| `scale` | float |  | 2.0 |

### `ui_tree`

Raw UI hierarchy (password fields masked). For debugging and skill authoring.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `raw` | bool |  | false |

### `find_elements`

Find elements by selector: text, text_any, text_contains, text_regex, desc, desc_contains, resource_id, role, index, near, within.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `selector` | dict | 예 |  |

### `get_device_info`

Device model, Android version, display size. No serial, IMEI or accounts.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

### `get_foreground_app`

Foreground app package/activity/label and whether it is a sensitive app.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

### `list_apps`

Installed packages (user apps by default).

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `filter` | str | null |  | null |
| `include_system` | bool |  | false |

### `get_gesture_profile`

Calibrated gesture defaults for this device (tap/long-press duration, swipe speed, curvature). Read-only.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

## 행동

### `tap`

Tap. Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `x_norm` | float | null |  | null |
| `y_norm` | float | null |  | null |
| `reason` | str |  |  |
| `return_observation` | bool |  | false |

### `double_tap`

Double tap. Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `x_norm` | float | null |  | null |
| `y_norm` | float | null |  | null |
| `reason` | str |  |  |

### `long_press`

Long press (default 800 ms). Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `x_norm` | float | null |  | null |
| `y_norm` | float | null |  | null |
| `duration_ms` | float |  | 800 |
| `reason` | str |  |  |

### `swipe`

Swipe from start=[x_norm,y_norm] to end=[x_norm,y_norm]. curve: ease|linear|ease_out.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `start` | list[float] | 예 |  |
| `end` | list[float] | 예 |  |
| `duration_ms` | float |  | 300 |
| `curve` | str |  | "ease" |
| `arc` | float |  | 0.0 |
| `reason` | str |  |  |

### `scroll`

Precise scroll without fling. direction: up|down|left|right (content direction). amount 0.1..1.0 of the container.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `direction` | str |  | "down" |
| `container` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `amount` | float |  | 0.6 |
| `reason` | str |  |  |

### `scroll_to`

Scroll until an element matching selector is visible.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `selector` | dict | 예 |  |
| `direction` | str |  | "down" |
| `max_swipes` | int |  | 10 |

### `fling`

Fast fling (imprecise; prefer scroll).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `direction` | str |  | "down" |
| `reason` | str |  |  |

### `drag`

Drag and drop: long-press the target then move to to=[x_norm,y_norm]. Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `to` | list[float] | 예 |  |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `x_norm` | float | null |  | null |
| `y_norm` | float | null |  | null |
| `hold_ms` | float |  | 600 |
| `reason` | str |  |  |

### `pinch`

Pinch zoom around center=[x_norm,y_norm]; scale>1 zooms in, <1 zooms out.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `center` | list[float] | null |  | null |
| `scale` | float |  | 2.0 |
| `reason` | str |  |  |

### `type_text`

Type text into the focused field (or tap the target first). Korean supported. Never use for secrets: use type_secret. Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `text` | str | 예 |  |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `clear` | bool |  | false |
| `submit` | bool |  | false |
| `reason` | str |  |  |

### `press_key`

Press a key: enter, back, home, app_switch, tab, del, forward_del, dpad_*, volume_*, power, search, move_home, move_end, page_up, page_down, copy, paste. meta: shift|ctrl|alt|meta.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `key` | str | 예 |  |
| `meta` | list[str] | null |  | null |
| `repeat` | int |  | 1 |
| `reason` | str |  |  |

### `system_action`

System action: home, back, recents, notifications, quick_settings, collapse_panels, screen_on, screen_off, rotate.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `action` | str | 예 |  |
| `reason` | str |  |  |

### `launch_app`

Launch an app by package (or known label).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `package` | str | null |  | null |
| `label` | str | null |  | null |
| `activity` | str | null |  | null |
| `reason` | str |  |  |

### `stop_app`

Force-stop an app.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `package` | str | 예 |  |
| `reason` | str |  |  |

### `open_url`

Open an http(s) URL on the phone, optionally in a given app package.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `url` | str | 예 |  |
| `package` | str | null |  | null |
| `reason` | str |  |  |

### `push_file`

Push a local file (from an allowed folder) to the phone's Download/phonepilot_tmp.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `local_path` | str | 예 |  |
| `remote_dir` | str | null |  | null |
| `reason` | str |  |  |

### `install_apk`

Install an APK from an allowed folder. Always requires strong user approval.

- 승인 게이트: 항상 강화 승인 (CONFIRM_HIGH) · 주석: destructive

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `local_path` | str | 예 |  |
| `reason` | str |  |  |

### `batch_actions`

Up to 10 simple actions on the same screen: [{tool, args}]. Sensitive actions make the whole batch fail.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `actions` | list[dict] | 예 |  |
| `stop_on_error` | bool |  | true |

## 대기

### `wait_for`

Wait until a condition holds: {text_appears|text_gone|element|package|activity_contains|keyboard_visible|screen_stable}.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `condition` | dict | 예 |  |
| `timeout_ms` | int |  | 10000 |

### `wait_stable`

Wait until the screen stops changing.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `timeout_ms` | int |  | 5000 |

### `sleep`

Sleep up to 10 seconds.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `ms` | int |  | 500 |

## 비밀값 (값은 절대 반환되지 않음)

### `list_secret_refs`

List secret reference names from .env (names, kinds, allowed apps). Values are never returned.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `scope` | str | null |  | null |

### `type_secret`

Type a secret (password/username) by ref into the focused field without revealing it. Only in allowed apps; passwords only into password fields. Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `ref` | str | 예 |  |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `submit` | bool |  | false |
| `reason` | str |  |  |

### `type_profile`

Type a non-secret profile value (name, birth date) by ref. Target: element+obs_id (preferred), selector, or x_norm/y_norm (0..1000).

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `ref` | str | 예 |  |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |
| `selector` | dict | null |  | null |
| `submit` | bool |  | false |
| `reason` | str |  |  |

### `secret_matches_screen`

Check whether a username/profile ref equals the text of an element (true/false only).

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `ref` | str | 예 |  |
| `element` | int | null |  | null |
| `obs_id` | str | null |  | null |

## 사람 개입·승인

### `request_human`

Pause and hand over to the human (kind: captcha, phone_verification, otp_entry, biometric, unlock, manual_step, question). Blocks until they press continue/abort.

- 승인 게이트: 사용자 안내 창을 띄우고 완료/취소까지 대기

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `kind` | str | 예 |  |
| `instructions` | str | 예 |  |
| `timeout_s` | float |  | 600 |

### `request_approval`

Ask the user to approve something you consider important (risk: low|medium|high).

- 승인 게이트: 사용자 승인 창을 띄움

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `title` | str | 예 |  |
| `summary` | str | 예 |  |
| `details` | dict | null |  | null |
| `risk` | str |  | "medium" |

### `notify_user`

Show a toast notification to the user.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `message` | str | 예 |  |
| `level` | str |  | "info" |

## 스킬·워크플로

### `list_skills`

List executable skills/workflows.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `tag` | str | null |  | null |

### `get_skill`

Get a skill's definition (steps, checkpoints).

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `name` | str | 예 |  |

### `run_workflow`

Start a workflow by skill name with inputs. Returns run_id; poll workflow_status.

- 승인 게이트: 없음 (폰 조작 없음)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `name` | str | 예 |  |
| `inputs` | dict | null |  | null |

### `workflow_status`

Workflow run status.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `run_id` | str | 예 |  |

### `workflow_control`

Control a workflow run: pause|resume|abort|skip_step (skip needs approval).

- 승인 게이트: skip_step만 사용자 승인

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `run_id` | str | 예 |  |
| `action` | str | 예 |  |

### `checkpoint`

Record a step checkpoint with a short evidence summary.

- 승인 게이트: 없음 (폰 조작 없음)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `run_id` | str | 예 |  |
| `step` | str | 예 |  |
| `evidence` | str |  |  |

### `capture_backup_codes`

Capture Google backup codes from the current screen into the recovery vault. Codes are never returned.

- 승인 게이트: 정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `account_ref` | str |  | "GOOGLE_MAIN" |

### `verify_2sv_methods`

On the Google 2-Step Verification page: list second-step methods and count phone-based ones.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

## 세션

### `session_info`

Connection, control state, lease, budgets, active workflows.

- 승인 게이트: 없음 (읽기 전용) · 주석: readOnly

### `release_control`

Release the control lease so another client may act.

- 승인 게이트: 없음 (폰 조작 없음)

### `end_session_tool`

End the session and run no-trace cleanup (asks the user first).

- 승인 게이트: 사용자 승인 창을 띄움

## 연동: YouTube (설정에서 켤 때만 노출)

### `yt_search_channels`

YouTube Data API: search channels by keyword (read-only, costs 100 quota units).

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `q` | str | 예 |  |
| `max_results` | int |  | 10 |

### `yt_channel_report`

YouTube Data API: channel statistics + recent video analysis (PRD 16.3).

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `channel_id` | str | 예 |  |

### `yt_category_list`

YouTube video categories for a region.

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `region` | str |  | "KR" |

## 연동: 거래소·증권 (설정에서 켤 때만 노출)

### `market_quote`

Quotes for symbols on an exchange (upbit: KRW-BTC, kis: 6-digit code).

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `exchange` | str | 예 |  |
| `symbols` | list[str] | 예 |  |

### `account_balances`

Account balances (read-only).

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `exchange` | str | 예 |  |

### `account_open_orders`

Open orders (read-only).

- 승인 게이트: 없음 (폰 조작 없음) · 주석: readOnly

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `exchange` | str | 예 |  |

### `order_draft`

Create an order draft and run risk checks. Nothing is sent to the exchange.

- 승인 게이트: 없음 (폰 조작 없음)

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `exchange` | str | 예 |  |
| `symbol` | str | 예 |  |
| `side` | str | 예 |  |
| `order_type` | str | 예 |  |
| `price` | str | null |  | null |
| `quantity` | str | null |  | null |
| `notional_krw` | str | null |  | null |

### `order_submit`

Submit a previously drafted order. Always requires strong user approval in the PhonePilot window.

- 승인 게이트: 항상 강화 승인 (CONFIRM_HIGH) · 주석: destructive

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `draft_id` | str | 예 |  |

### `order_cancel`

Cancel an open order. Requires user approval.

- 승인 게이트: 항상 강화 승인 (CONFIRM_HIGH) · 주석: destructive

| 인자 | 타입 | 필수 | 기본값 |
| --- | --- | --- | --- |
| `exchange` | str | 예 |  |
| `order_id` | str | 예 |  |
