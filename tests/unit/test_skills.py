"""TC-SKL-01 and skill registry security rules (PRD 11.4)."""

from pathlib import Path

import pytest

from phonepilot.skills.loader import SkillLoadError, SkillRegistry, parse

REPO = Path(__file__).resolve().parents[2]


def test_bundled_skills_load_cleanly():
    reg = SkillRegistry(REPO / "skills")
    reg.reload()
    assert reg.errors == {}
    for name in ["google.account_with_totp", "youtube.create_channel", "youtube.channel_launch_checklist", "android.dismiss_common_popups"]:
        assert name in reg.skills


def test_account_skill_is_sensitive_and_limited():
    reg = SkillRegistry(REPO / "skills")
    reg.reload()
    s = reg.get("google.account_with_totp").skill
    assert s.sensitive and s.account_creation
    ids = [st.id for st in s.steps]
    assert "capture_codes" in ids and ids[-1] == "verify_methods"


BASE = """
name: t.skill
steps:
"""


def test_secret_template_rejected_tc_skl_01():
    text = """
name: t.bad
inputs:
  pw: {type: secret_ref, default: X_PASSWORD}
steps:
  - id: s1
    action: type_text
    args: {text: "{{inputs.pw}}"}
    expect: {text: ok}
"""
    with pytest.raises(SkillLoadError):
        parse(text)


def test_missing_expect_warns():
    loaded = parse(BASE + "  - id: a\n    action: tap\n    args: {selector: {text: x}}\n")
    assert any("without expect" in w for w in loaded.warnings)


def test_engagement_loop_rejected():
    text = BASE + """  - id: farm
    type: loop
    max_iter: 50
    steps:
      - id: like
        action: tap
        args: {selector: {text: "좋아요"}}
      - id: next
        action: swipe
        args: {start: [500, 800], end: [500, 200]}
"""
    with pytest.raises(SkillLoadError, match="engagement"):
        parse(text)


def test_agent_step_forbidden_tools():
    text = BASE + "  - id: a\n    type: agent_step\n    goal: x\n    allowed_tools: [tap, install_apk]\n"
    with pytest.raises(SkillLoadError):
        parse(text)


def test_unknown_fields_rejected():
    with pytest.raises(SkillLoadError):
        parse(BASE + "  - id: a\n    action: tap\n    disable_policy: true\n")


def test_user_skills_need_approval(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "mine.yaml").write_text("name: user.mine\nsteps:\n  - id: a\n    action: sleep\n    args: {ms: 1}\n    optional: true\n", encoding="utf-8")
    reg = SkillRegistry(REPO / "skills", user, tmp_path / "approved.json")
    reg.reload()
    assert "user.mine" not in reg.skills and "user.mine" in reg.pending_user
    reg.approve(reg.pending_user["user.mine"].sha256)
    assert "user.mine" in reg.skills
    (user / "mine.yaml").write_text("name: user.mine\nsteps:\n  - id: b\n    action: sleep\n    args: {ms: 2}\n    optional: true\n", encoding="utf-8")
    reg.reload()
    assert "user.mine" not in reg.skills  # content changed -> approval needed again
