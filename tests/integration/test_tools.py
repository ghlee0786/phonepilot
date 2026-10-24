"""Tool pipeline on FakeDevice: TC-INP-04..06, TC-SEC-04..06, TC-HIL-01..05/07, TC-MIR-08, TC-SKL-03/04."""

import asyncio

import pytest

from phonepilot.core import gesture
from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError
from phonepilot.security.approvals import ApprovalResponse

from tests.conftest import CANARY_EMAIL, CANARY_PASSWORD


async def goto(stack, screen):
    stack.device.goto(screen)
    return await stack.tools.observe()


async def test_observe_then_tap_navigates(stack):
    obs = await stack.tools.observe()
    assert obs["package"] == "com.android.launcher3"
    res = await stack.tools.tap(element=1, obs_id=obs["obs_id"], reason="설정 열기")
    assert res["ok"] and res["screen_changed"] and stack.device.current == "settings"
    assert stack.arbiter.open_pointers == {}
    touches = [m for m in stack.device.decoded if m["type"] == p.ControlType.INJECT_TOUCH_EVENT]
    assert [m["action"] for m in touches] == [p.MotionAction.DOWN, p.MotionAction.UP]
    assert all((m["screen_w"], m["screen_h"]) == (540, 1200) for m in touches)  # frame coordinates


async def test_stale_element(stack):
    obs = await stack.tools.observe()
    stack.device.goto("store")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(element=3, obs_id=obs["obs_id"])
    assert e.value.code == "ELEMENT_STALE"


async def test_selector_and_coordinate_taps(stack):
    await stack.tools.tap(selector={"text": "YouTube"})
    assert stack.device.current == "youtube"
    stack.device.goto("home")
    await stack.tools.observe()
    await stack.tools.tap(x_norm=500, y_norm=192)  # center of '설정'
    assert stack.device.current == "settings"


async def test_human_input_interrupts_ai_gesture_tc_inp_04(stack):
    task = asyncio.create_task(stack.arbiter.ai_gesture(gesture.long_press(500, 1500, duration_ms=1500)))
    await asyncio.sleep(0.2)
    await stack.arbiter.human_touch(p.MotionAction.DOWN, 10, 10)
    await stack.arbiter.human_touch(p.MotionAction.UP, 10, 10)
    with pytest.raises(PhonePilotError) as e:
        await task
    assert e.value.code == "HUMAN_ACTIVE"
    assert stack.arbiter.open_pointers == {}
    with pytest.raises(PhonePilotError):
        await stack.tools.tap(selector={"text": "설정"})


async def test_kill_switch_tc_inp_05(stack):
    task = asyncio.create_task(stack.arbiter.ai_gesture(gesture.long_press(500, 1500, duration_ms=3000)))
    await asyncio.sleep(0.1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await stack.arbiter.kill()
    with pytest.raises(PhonePilotError) as e:
        await task
    assert loop.time() - t0 < 0.1
    assert e.value.code == "KILL_SWITCH_ACTIVE"
    assert stack.arbiter.open_pointers == {}
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(selector={"text": "설정"})
    assert e.value.code == "KILL_SWITCH_ACTIVE"
    obs = await stack.tools.observe()  # observation still allowed
    assert obs["package"]
    stack.arbiter.allow_ai()
    await stack.tools.tap(selector={"text": "설정"})


async def test_korean_text_via_clipboard_tc_inp_06(stack):
    await goto(stack, "youtube_create")
    stack.tools.device_clipboard = "이전 내용"
    res = await stack.tools.type_text(text="안녕하세요 채널", selector={"resource_id": "com.google.android.youtube:id/name"})
    assert stack.device.field_value("youtube_create", "com.google.android.youtube:id/name") == "안녕하세요 채널"
    clips = [m for m in stack.device.decoded if m["type"] == p.ControlType.SET_CLIPBOARD]
    assert clips[0]["paste"] is True and clips[-1]["text"] == "이전 내용" and clips[-1]["paste"] is False
    assert res["verified"] is True


async def test_korean_text_waits_for_slow_paste_before_restoring_clipboard(stack):
    # real device: with clear=True the paste key sits behind the DEL keys, so the app reads the clipboard late
    await goto(stack, "youtube_create")
    stack.tools.device_clipboard = "이전 내용"
    stack.device.paste_delay_s = 0.5
    rid = "com.google.android.youtube:id/name"
    await stack.tools.type_text(text="old", selector={"resource_id": rid})
    res = await stack.tools.type_text(text="새 채널 이름", clear=True, selector={"resource_id": rid})
    assert stack.device.field_value("youtube_create", rid) == "새 채널 이름"
    assert stack.device.clipboard == "이전 내용"
    assert res["verified"] is True


async def test_ascii_text_uses_inject_text(stack):
    await goto(stack, "youtube_create")
    await stack.tools.type_text(text="techlog_bite", selector={"resource_id": "com.google.android.youtube:id/handle"})
    assert stack.device.field_value("youtube_create", "com.google.android.youtube:id/handle") == "techlog_bite"
    assert not any(m["type"] == p.ControlType.SET_CLIPBOARD for m in stack.device.decoded)


async def test_type_secret_happy_path_tc_sec_05(stack):
    await goto(stack, "google_signin")
    r1 = await stack.tools.type_secret(ref="GOOGLE_MAIN_EMAIL", selector={"resource_id": "com.google.android.gms:id/email"})
    r2 = await stack.tools.type_secret(ref="GOOGLE_MAIN_PASSWORD", selector={"resource_id": "com.google.android.gms:id/password"})
    assert stack.device.field_value("google_signin", "com.google.android.gms:id/password") == CANARY_PASSWORD
    assert r2["typed"] and r2["field_is_password"] and r2["length"] == len(CANARY_PASSWORD)
    for r in (r1, r2):
        assert CANARY_PASSWORD not in repr(r) and CANARY_EMAIL not in repr(r)
    assert not any(m["type"] == p.ControlType.SET_CLIPBOARD for m in stack.device.decoded)
    assert any(m["type"] == p.ControlType.INJECT_TEXT for m in stack.device.decoded)


async def test_type_secret_scope_checks_tc_sec_04(stack):
    # wrong app
    await goto(stack, "youtube_create")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.type_secret(ref="GOOGLE_MAIN_PASSWORD", selector={"resource_id": "com.google.android.youtube:id/name"})
    assert e.value.code == "SECRET_SCOPE_DENIED"
    # password into a non-password field
    await goto(stack, "google_signin")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.type_secret(ref="GOOGLE_MAIN_PASSWORD", selector={"resource_id": "com.google.android.gms:id/email"})
    assert e.value.code == "SECRET_SCOPE_DENIED"
    # api keys are never typed on screen
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.type_secret(ref="UPBIT_MAIN_SECRET_KEY", selector={"resource_id": "com.google.android.gms:id/password"})
    assert e.value.code == "SECRET_SCOPE_DENIED"
    assert stack.device.field_value("google_signin", "com.google.android.gms:id/password") == ""


async def test_type_secret_toctou_app_switch(stack, monkeypatch):
    await goto(stack, "google_signin")
    real_fg = stack.device.foreground
    calls = {"n": 0}

    async def switching_fg():
        calls["n"] += 1
        if calls["n"] >= 2:  # the app changes between the check and the injection
            return "com.evil.app", ".Phish"
        return await real_fg()

    monkeypatch.setattr(stack.device, "foreground", switching_fg)
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.type_secret(ref="GOOGLE_MAIN_PASSWORD", selector={"resource_id": "com.google.android.gms:id/password"})
    assert e.value.code == "SECRET_SCOPE_DENIED"
    assert stack.device.field_value("google_signin", "com.google.android.gms:id/password") == ""


async def test_type_text_refuses_secret_values_tc_sec_06(stack):
    await goto(stack, "google_signin")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.type_text(text=CANARY_PASSWORD)
    assert e.value.code == "USE_TYPE_SECRET"
    with pytest.raises(PhonePilotError):
        await stack.tools.type_text(text=f"my password is {CANARY_PASSWORD}")


async def test_purchase_needs_strong_approval_tc_hil_01(stack, approver):
    obs = await goto(stack, "store")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(element=1, obs_id=obs["obs_id"])
    assert e.value.code == "APPROVAL_DENIED"
    assert stack.device.current == "store"
    assert not [m for m in stack.device.decoded if m["type"] == p.ControlType.INJECT_TOUCH_EVENT]
    req = approver.requests[-1]
    assert req.high and req.decision.category == "payment" and req.expected_amount == "₩4,400"
    assert req.preview_png and req.preview_png[:4] == b"\x89PNG"

    # approving without retyping the amount is not enough
    approver.answer = lambda r: ApprovalResponse(True, typed_confirmation="승인")
    with pytest.raises(PhonePilotError):
        await stack.tools.tap(selector={"text": "구매"})
    assert stack.device.current == "store"

    approver.approve_all()
    res = await stack.tools.tap(selector={"text": "구매"})
    assert res["approval"]["level"] == "CONFIRM_HIGH"
    assert stack.device.current == "store_done"


async def test_coordinate_tap_is_classified_like_the_element_tc_hil_02(stack, approver):
    await goto(stack, "store")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(x_norm=500, y_norm=187)  # the '구매' button by coordinates
    assert e.value.code == "APPROVAL_DENIED"
    assert stack.device.current == "store"


async def test_batch_with_sensitive_action_rejected_tc_hil_03(stack, approver):
    await goto(stack, "store")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.batch_actions(actions=[{"tool": "press_key", "args": {"key": "tab"}}, {"tool": "tap", "args": {"selector": {"text": "구매"}}}])
    assert e.value.code == "POLICY_BLOCKED"
    assert not approver.requests and stack.device.current == "store"


async def test_approval_invalidated_when_screen_changes_tc_hil_04(stack, approver):
    await goto(stack, "store")

    def approve_but_change(req):
        stack.device.goto("home")  # a popup / navigation happened while the dialog was open
        return ApprovalResponse(True, typed_confirmation=req.expected_amount or "승인")

    approver.answer = approve_but_change
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(selector={"text": "구매"})
    assert e.value.code == "APPROVAL_INVALIDATED"
    assert stack.device.current == "home"


async def test_approval_timeout_denies_tc_hil_05(stack):
    stack.approvals.timeout_s = 0.2

    async def never(_req):
        await asyncio.sleep(10)

    stack.approvals.approval_handler = never
    await goto(stack, "store")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(selector={"text": "구매"})
    assert e.value.code == "APPROVAL_TIMEOUT"
    assert stack.device.current == "store"


async def test_engagement_confirm_and_daily_limit(stack, approver):
    approver.approve_all()
    stack.tools.config.engagement_daily = 1
    await goto(stack, "youtube")
    await stack.tools.tap(selector={"text": "좋아요"})
    assert approver.requests[-1].decision.category == "engagement"
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(selector={"text": "구독"})
    assert e.value.code == "LIMIT_EXCEEDED"


async def test_phone_2sv_blocked_tc_hil_07(stack, approver):
    approver.approve_all()
    await goto(stack, "google_2sv")
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.tap(selector={"text": "전화번호 추가"})
    assert e.value.code == "POLICY_BLOCKED"
    assert not approver.requests


async def test_secure_surface_tc_mir_08(stack):
    obs = await goto(stack, "bank")
    assert obs["secure_surface"] is True and "SECURE_SURFACE" in obs["warnings"]


async def test_sensitive_screen_withholds_images_and_codes(stack):
    await goto(stack, "backup_codes")
    obs = await stack.tools.observe(image="som")
    assert obs["sensitive_screen"] is True and "_image" not in obs
    assert "1000 2000" not in str(obs)  # backup codes are masked in the observation
    with pytest.raises(PhonePilotError):
        await stack.tools.screenshot()


async def test_capture_backup_codes_tc_skl_03(stack, tmp_path):
    await goto(stack, "backup_codes")
    res = await stack.tools.capture_backup_codes(account_ref="GOOGLE_MAIN")
    assert res == {"saved": True, "count": 10, "path_hint": res["path_hint"]}
    assert "1000" not in str(res)
    files = list((tmp_path / "vault").glob("google_MAIN_backup-codes_*.txt"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "1000 2000" in content and CANARY_EMAIL in content
    assert "1000 2000" not in str(stack.timeline.recent())


async def test_verify_2sv_methods_tc_skl_04(stack):
    await goto(stack, "google_2sv_on")
    ok = await stack.tools.verify_2sv_methods()
    assert ok["phone_methods"] == 0 and {"authenticator", "backup_codes"} <= set(ok["methods"])
    stack.device.screens["google_2sv_on"].nodes.append(__import__("phonepilot.testing.fake_device", fromlist=["label"]).label("음성 또는 문자 메시지", 600))
    stack.device.render()
    bad = await stack.tools.verify_2sv_methods()
    assert bad["phone_methods"] == 1


async def test_rate_limit_and_step_budget(stack):
    stack.tools.config.step_budget = 2
    await stack.tools.press_key(key="tab", wait=False)
    await stack.tools.press_key(key="tab", wait=False)
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.press_key(key="tab", wait=False)
    assert e.value.code == "STEP_BUDGET_EXCEEDED"


async def test_control_lease(stack):
    await stack.tools.press_key(actor="mcp:a", key="tab", wait=False)
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.press_key(actor="mcp:b", key="tab", wait=False)
    assert e.value.code == "CONTROL_BUSY"
    await stack.tools.release_control(actor="mcp:a")
    await stack.tools.press_key(actor="mcp:b", key="tab", wait=False)


async def test_wait_for_and_scroll_to(stack):
    res = await stack.tools.wait_for(condition={"text_appears": "홈 화면"}, timeout_ms=1000)
    assert res["met"]
    res = await stack.tools.wait_for(condition={"package": "com.nope"}, timeout_ms=300)
    assert not res["met"]
    found = await stack.tools.scroll_to(selector={"text": "YouTube"})
    assert found["found"] and found["swipes"] == 0


async def test_open_url_scheme_blocked(stack):
    with pytest.raises(PhonePilotError) as e:
        await stack.tools.open_url(url="intent://scan/#Intent;scheme=zxing;end")
    assert e.value.code == "POLICY_BLOCKED"
    await stack.tools.open_url(url="https://example.com")
    assert any("VIEW -d https://example.com" in c for c in stack.device.shell_log)


async def test_push_file_restricted(stack, tmp_path):
    outside = tmp_path / "x.png"
    outside.write_bytes(b"png")
    with pytest.raises(PhonePilotError):
        await stack.tools.push_file(local_path=str(outside))
    allowed = tmp_path / "assets"
    allowed.mkdir()
    f = allowed / "pp_profile.png"
    f.write_bytes(b"png")
    stack.tools.config.allowed_push_dirs = [allowed]
    res = await stack.tools.push_file(local_path=str(f))
    assert res["remote_path"].endswith("phonepilot_tmp/pp_profile.png")
    assert stack.device.pushed[res["remote_path"]] == b"png"


async def test_timeline_records_without_secrets(stack):
    await goto(stack, "google_signin")
    await stack.tools.type_secret(ref="GOOGLE_MAIN_PASSWORD", selector={"resource_id": "com.google.android.gms:id/password"})
    events = stack.timeline.recent()
    assert any(e["tool"] == "type_secret" for e in events)
    assert CANARY_PASSWORD not in str(events)


async def test_kill_cancels_pending_approval(stack):
    gate = asyncio.Event()

    async def slow(_req):
        await gate.wait()
        return ApprovalResponse(True, typed_confirmation="4400")

    stack.approvals.approval_handler = slow
    await goto(stack, "store")
    task = asyncio.create_task(stack.tools.tap(selector={"text": "구매"}))
    for _ in range(50):
        if stack.approvals.pending:
            break
        await asyncio.sleep(0.02)
    await stack.arbiter.kill()
    stack.approvals.deny_pending()
    with pytest.raises(PhonePilotError) as e:
        await asyncio.wait_for(task, 1)
    assert e.value.code == "APPROVAL_DENIED"
    assert stack.device.current == "store"


async def test_deny_with_stop_all_triggers_kill(stack, approver):
    killed = []

    async def on_stop():
        killed.append(True)
        await stack.arbiter.kill()

    stack.approvals.on_stop_all = on_stop
    approver.answer = lambda r: ApprovalResponse(False, stop_all=True)
    await goto(stack, "store")
    with pytest.raises(PhonePilotError):
        await stack.tools.tap(selector={"text": "구매"})
    assert killed and stack.arbiter.state.value == "killed"


async def test_timeline_has_tap_point(stack):
    await stack.tools.tap(selector={"text": "설정"})
    ev = [e for e in stack.timeline.recent() if e["tool"] == "tap"][-1]
    assert 0 <= ev["detail"]["x_norm"] <= 1000 and 0 <= ev["detail"]["y_norm"] <= 1000
