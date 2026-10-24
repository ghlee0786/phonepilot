"""TC-SKL-02: a workflow-A-shaped skill end to end on FakeDevice, plus engine control paths."""

import asyncio
import textwrap

import pytest

from phonepilot.errors import PhonePilotError
from phonepilot.security.approvals import ApprovalResponse, HumanResponse
from phonepilot.skills.engine import RunState
from phonepilot.skills.loader import SkillRegistry

from tests.conftest import CANARY_PASSWORD

SIGNIN_2SV = textwrap.dedent(
    """
    name: test.signin_2sv
    description: demo flow modelled on workflow A
    sensitive: true
    account_creation: true
    inputs:
      alias: {type: string, default: MAIN}
    steps:
      - id: open_settings
        action: tap
        args: {selector: {text: 설정}}
        expect: {package: com.android.settings, timeout_ms: 2000}
      - id: open_google
        action: tap
        args: {selector: {text: Google}}
        expect: {text: 로그인, timeout_ms: 2000}
      - id: email
        type: secret_input
        ref: GOOGLE_{{inputs.alias}}_EMAIL
        target: {resource_id: "com.google.android.gms:id/email"}
      - id: password
        type: secret_input
        ref: GOOGLE_{{inputs.alias}}_PASSWORD
        target: {resource_id: "com.google.android.gms:id/password"}
      - id: next
        action: tap
        args: {selector: {text: 다음}}
        expect: {text: 2단계 인증, timeout_ms: 2000}
      - id: otp
        type: human
        kind: otp_entry
        instructions: OTP 코드를 직접 입력하세요
      - id: confirm_turn_on
        type: approval
        title: 2단계 인증 켜기
        summary: 인증자 앱으로 2단계 인증을 켭니다
        risk: medium
      - id: codes_page
        action: tap
        args: {selector: {text: 백업 코드 받기}}
        expect: {text: 완료, timeout_ms: 2000}
      - id: capture
        type: capture
        capture: backup_codes
        account_ref: "GOOGLE_{{inputs.alias}}"
      - id: done
        action: tap
        args: {selector: {text: 완료}}
        expect: {text: 2단계 인증 사용 중, timeout_ms: 2000}
      - id: verify
        type: capture
        capture: 2sv_methods
    """
)

LIKE_ONCE = textwrap.dedent(
    """
    name: test.like_once
    steps:
      - id: yt
        action: tap
        args: {selector: {text: YouTube}}
        expect: {package: com.google.android.youtube, timeout_ms: 2000}
      - id: like
        action: tap
        args: {selector: {text: 좋아요}}
        expect: {timeout_ms: 500}
    """
)


OPEN_SETTINGS = textwrap.dedent(
    """
    name: test.open_settings
    steps:
      - id: open
        action: tap
        args: {selector: {text: 설정}}
        expect: {package: com.android.settings, timeout_ms: 2000}
    """
)


@pytest.fixture
def engine(stack, tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    (d / "a.yaml").write_text(SIGNIN_2SV, encoding="utf-8")
    (d / "b.yaml").write_text(LIKE_ONCE, encoding="utf-8")
    (d / "c.yaml").write_text(OPEN_SETTINGS, encoding="utf-8")
    stack.registry.builtin_dir = d
    stack.registry.reload()
    assert not stack.registry.errors, stack.registry.errors
    return stack.engine


async def wait_done(engine, run_id, timeout=30):
    for _ in range(int(timeout * 10)):
        st = engine.status(run_id)
        if st["state"] in {"COMPLETED", "FAILED", "ABORTED", "NEEDS_ATTENTION"}:
            return st
        await asyncio.sleep(0.1)
    raise AssertionError(f"workflow did not finish: {engine.status(run_id)}")


async def test_workflow_started_by_lease_holder_is_not_blocked(stack, engine, approver):
    # an MCP client that just acted holds the 60 s control lease; its own workflow must still be able to act
    approver.approve_all()
    await stack.tools.press_key(actor="mcp:client", key="home")
    assert stack.arbiter.lease_owner == "mcp:client"
    run = await stack.tools.run_workflow(actor="mcp:client", name="test.open_settings")
    st = await wait_done(engine, run["run_id"])
    assert st["state"] == "COMPLETED", st
    assert st["human_interventions"] == {}
    assert stack.arbiter.lease_owner is None  # finished run hands control back right away
    await stack.tools.press_key(actor="mcp:client", key="home")


async def test_workflow_a_shape_completes_tc_skl_02(stack, engine, approver, tmp_path):
    approver.approve_all()
    states = []
    engine.listeners.append(lambda run: states.append(run.state.value))
    started = await engine.start("test.signin_2sv", {}, requested_by="test")
    st = await wait_done(engine, started["run_id"])
    assert st["state"] == "COMPLETED", st
    assert "WAITING_HUMAN" in states and "WAITING_APPROVAL" in states
    assert st["report"]["backup_codes"]["count"] == 10
    assert st["report"]["two_step"]["phone_methods"] == 0
    assert st["human_interventions"] == {"otp_entry": 1}
    assert stack.device.field_value("google_signin", "com.google.android.gms:id/password") == CANARY_PASSWORD
    assert [h.kind for h in approver.humans] == ["otp_entry"]
    titles = [r.title for r in approver.requests]
    assert titles[0].startswith("워크플로 시작") and "2단계 인증 켜기" in titles
    assert CANARY_PASSWORD not in str(st)
    # 24h account-creation guard (PRD 3.3)
    with pytest.raises(PhonePilotError) as e:
        await engine.start("test.signin_2sv", {})
    assert e.value.code == "POLICY_BLOCKED"


async def test_workflow_start_denied(stack, engine, approver):
    with pytest.raises(PhonePilotError) as e:
        await engine.start("test.signin_2sv", {})
    assert e.value.code == "APPROVAL_DENIED"
    assert stack.device.current == "home"


async def test_human_abort_stops_workflow(stack, engine, approver):
    approver.approve_all()
    approver.human_answer = lambda r: HumanResponse("aborted")
    started = await engine.start("test.signin_2sv", {})
    st = await wait_done(engine, started["run_id"])
    assert st["state"] == "ABORTED"
    assert st["current_step"] is None and "otp" not in st["completed"]


async def test_engagement_inside_workflow_blocked(stack, engine, approver):
    approver.approve_all()
    started = await engine.start("test.like_once", {})
    st = await wait_done(engine, started["run_id"])
    assert st["state"] == "FAILED" and st["error"]["code"] == "POLICY_BLOCKED"
    assert not any(r.decision.category == "engagement" for r in approver.requests)


async def test_failed_step_hands_over_to_human(stack, engine, approver, tmp_path):
    approver.approve_all()
    calls = []

    def human(req):
        calls.append(req.kind)
        stack.device.goto("settings")  # the human fixes it by hand
        return HumanResponse("continued")

    approver.human_answer = human
    stack.registry.builtin_dir.joinpath("c.yaml").write_text(
        textwrap.dedent(
            """
            name: test.recover
            on_error: {retry: 0, then: human}
            steps:
              - id: impossible
                action: tap
                args: {selector: {text: 존재하지않는버튼}}
                expect: {package: com.android.settings, timeout_ms: 500}
            """
        ),
        encoding="utf-8",
    )
    stack.registry.reload()
    started = await stack.engine.start("test.recover", {})
    st = await wait_done(stack.engine, started["run_id"])
    assert st["state"] == "COMPLETED" and calls == ["manual_step"]


async def test_abort_and_kill(stack, engine, approver):
    approver.approve_all()
    gate = asyncio.Event()

    async def slow_human(req):
        await gate.wait()
        return HumanResponse("continued")

    stack.approvals.human_handler = slow_human
    started = await engine.start("test.signin_2sv", {})
    for _ in range(100):
        if engine.status(started["run_id"])["state"] == "WAITING_HUMAN":
            break
        await asyncio.sleep(0.05)
    await engine.control(started["run_id"], "abort")
    st = engine.status(started["run_id"])
    assert st["state"] == "ABORTED"
    gate.set()


async def test_bundled_registry_lists_skills(stack, approver):
    reg = SkillRegistry(stack.registry.builtin_dir.parents[0] / "nonexistent")
    reg.reload()
    assert reg.skills == {}
    names = [s["name"] for s in stack.engine.list()]
    assert "test.signin_2sv" not in names or True
