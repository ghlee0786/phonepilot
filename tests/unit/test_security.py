"""TC-SEC-01..03, policy classification (13.2/13.9), limits (TC-HIL-08/09), recovery vault."""

import json
import pickle
from datetime import datetime, timedelta

import pytest

from phonepilot.errors import PhonePilotError
from phonepilot.security.limits import KST, DailyCounters, RateLimiter
from phonepilot.security.policy import ActionContext, Level, PolicyEngine
from phonepilot.security.redactor import Redactor
from phonepilot.security.secrets import SecretVault
from phonepilot.security.vault import RecoveryVault, extract_backup_codes

from tests.conftest import CANARY_API, CANARY_EMAIL, CANARY_PASSWORD


def test_secret_cannot_be_serialized_tc_sec_01(vault: SecretVault):
    s = vault.get("GOOGLE_MAIN_PASSWORD")
    for text in (repr(s), str(s), f"{s}", "%s" % s, f"{s!r:>40}"):
        assert CANARY_PASSWORD not in text
    with pytest.raises(TypeError):
        json.dumps(s)
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with s.reveal() as buf:
        assert bytes(buf) == CANARY_PASSWORD.encode()
        copy = buf
    assert set(copy) == {0}  # the revealed copy is zeroed


def test_secret_refs_never_contain_values(vault: SecretVault):
    dump = json.dumps(vault.refs(), ensure_ascii=False)
    assert CANARY_PASSWORD not in dump and CANARY_EMAIL not in dump and CANARY_API not in dump
    with pytest.raises(PhonePilotError) as e:
        vault.get("NOPE")
    assert e.value.code == "SECRET_NOT_FOUND"


def test_vault_zero_all(vault: SecretVault):
    s = vault.get("GOOGLE_MAIN_PASSWORD")
    vault.zero_all()
    assert not s and not vault.has("GOOGLE_MAIN_PASSWORD")


def test_env_file_loading(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A_PASSWORD=abc12345\nB_EMAIL=me@example.com\nC_KEY=__set_me__\n", encoding="utf-8")
    (tmp_path / "secrets.meta.yaml").write_text("B_EMAIL: {kind: username, allowed_packages: [x.y]}\n", encoding="utf-8")
    v = SecretVault.from_files(env)
    kinds = {r["ref"]: r["kind"] for r in v.refs()}
    assert kinds == {"A_PASSWORD": "password", "B_EMAIL": "username", "C_KEY": "api_key"}
    assert any("C_KEY" in w for w in v.warnings)


def test_redactor_values_and_variants_tc_sec_02(vault: SecretVault):
    import base64
    import urllib.parse

    r = Redactor(vault)
    text = f"pw={CANARY_PASSWORD} url={urllib.parse.quote(CANARY_PASSWORD, safe='')} b64={base64.b64encode(CANARY_PASSWORD.encode()).decode()} mail {CANARY_EMAIL}"
    out = r.redact(text)
    assert CANARY_PASSWORD not in out and CANARY_EMAIL not in out
    assert "%23" not in out.split("url=")[1].split()[0] or "REDACTED" in out
    assert out.count("[REDACTED:") >= 4
    assert r.redact_obj({"a": [text]})["a"][0] == out


def test_redactor_patterns_tc_sec_03():
    r = Redactor()
    assert "[REDACTED:card]" in r.redact("card 4111 1111 1111 1111 exp")
    assert "1234 5678 9012 3456" in r.redact("order 1234 5678 9012 3456")  # fails Luhn -> kept
    assert "[REDACTED:rrn]" in r.redact("주민 900101-1234567")
    assert "[REDACTED:otpauth]" in r.redact("otpauth://totp/Google:me?secret=JBSWY3DPEHPK3PXP")
    assert "[REDACTED:totp_key]" in r.redact("설정 키: abcd ef23 ghij kl45 mnop qr67 stuv wx23")
    assert "[REDACTED:code]" in r.redact("1234 5678", sensitive_context=True)
    assert "1234 5678" in r.redact("1234 5678")  # not masked outside sensitive screens
    assert "[REDACTED:otp]" in r.redact("코드 123456", sensitive_context=True)


def ctx(**kw):
    base = dict(tool="tap", package="com.example", app_label="example", target_label="", screen_texts=[])
    base.update(kw)
    return ActionContext(**base)


@pytest.mark.parametrize(
    "c,level,category",
    [
        (ctx(target_label="설정"), Level.ALLOW, "general"),
        (ctx(tool="observe"), Level.ALLOW, "read_only"),
        (ctx(target_label="구매", package="com.android.vending"), Level.CONFIRM_HIGH, "payment"),
        (ctx(target_label="Buy now"), Level.CONFIRM_HIGH, "payment"),
        (ctx(target_label="매수"), Level.CONFIRM_HIGH, "trading"),
        (ctx(target_label="송금"), Level.BLOCK, "transfer"),
        (ctx(target_label="출금하기"), Level.BLOCK, "transfer"),
        (ctx(target_label="2단계 인증 사용 중지"), Level.CONFIRM_HIGH, "account_security"),
        (ctx(target_label="좋아요", package="com.google.android.youtube"), Level.CONFIRM, "engagement"),
        (ctx(target_label="구독", package="com.google.android.youtube", in_workflow=True), Level.BLOCK, "engagement"),
        (ctx(target_label="좋아요", in_batch=True), Level.BLOCK, "engagement"),
        (ctx(target_label="전화번호 추가", screen_texts=["2단계 인증"]), Level.BLOCK, "phone_2sv"),
        (ctx(target_label="다음", screen_texts=["로봇이 아닙니다"]), Level.BLOCK, "captcha"),
        (ctx(target_label="다음", screen_texts=["프리미엄", "₩4,400"]), Level.CONFIRM_HIGH, "payment"),
        (ctx(target_label="보내기"), Level.CONFIRM, "outbound"),
        (ctx(target_label="허용", package="com.google.android.permissioncontroller"), Level.CONFIRM, "permission"),
        (ctx(target_label="잔액", package="viva.republica.toss"), Level.CONFIRM, "sensitive_app"),
        (ctx(target_label="메뉴", app_label="KB스타뱅킹"), Level.CONFIRM, "sensitive_app"),
        (ctx(tool="install_apk"), Level.CONFIRM_HIGH, "money_or_install"),
        (ctx(tool="open_url", args={"url": "intent://evil"}), Level.BLOCK, "url_scheme"),
        (ctx(tool="tap", secure_surface=True, coordinate_tap=True), Level.CONFIRM, "secure_surface"),
    ],
)
def test_policy_classification(c, level, category):
    d = PolicyEngine().classify(c)
    assert d.level == level, d.reasons
    if level != Level.ALLOW or category != "general":
        assert d.category == category


def test_policy_user_keywords_extend_but_cannot_remove():
    pe = PolicyEngine(keywords={"payment": ["특별결제"], "transfer": []})
    assert pe.classify(ctx(target_label="특별결제")).level == Level.CONFIRM_HIGH
    assert pe.classify(ctx(target_label="송금")).level == Level.BLOCK


def test_rate_limiter_tc_hil_09():
    now = [0.0]
    rl = RateLimiter(5, 120, clock=lambda: now[0])
    for _ in range(5):
        rl.check()
    with pytest.raises(PhonePilotError) as e:
        rl.check()
    assert e.value.code == "RATE_LIMITED"
    now[0] = 1.5
    rl.check()


def test_daily_counters_and_account_creation(tmp_path):
    t = [datetime(2026, 10, 12, 23, 0, tzinfo=KST)]
    path = tmp_path / "limits.json"
    c = DailyCounters(path, now=lambda: t[0])
    for _ in range(2):
        c.use_engagement(2)
    with pytest.raises(PhonePilotError):
        c.use_engagement(2)
    with pytest.raises(PhonePilotError):
        c.use_purchase(1000, 0)  # default purchase limit 0 = disabled
    c.record_account_creation()
    with pytest.raises(PhonePilotError):
        DailyCounters(path, now=lambda: t[0] + timedelta(hours=5)).check_account_creation()
    t[0] = t[0] + timedelta(hours=25)
    c2 = DailyCounters(path, now=lambda: t[0])
    c2.check_account_creation()
    c2.use_engagement(2)  # new KST day resets the counter
    saved = json.loads(path.read_text())
    assert set(saved) == {"date", "engagement", "purchase_krw", "last_account_creation"}  # aggregates only


def test_backup_code_extraction_and_vault(tmp_path):
    texts = ["백업 코드", "1234 5678", "2345-6789", "3456 7890\n4567 8901", "5678 9012", "6789 0123", "7890 1234", "8901 2345", "9012 3456", "0123 4567", "1234 5678"]
    codes = extract_backup_codes(texts)
    assert len(codes) == 10 and codes[1] == "2345 6789"
    v = RecoveryVault(tmp_path / "vault")
    p1 = v.save("MAIN", "me@example.com", codes, now=datetime(2026, 10, 12, 10, 0))
    assert p1.name == "google_MAIN_backup-codes_20261012-1000.txt"
    assert extract_backup_codes(p1.read_text(encoding="utf-8").splitlines()) == codes
    p2 = v.save("MAIN", "me@example.com", codes, now=datetime(2026, 10, 12, 11, 0))
    assert p2.exists() and not p1.exists()
    assert any("_superseded" in f.name for f in (tmp_path / "vault").iterdir())
    with pytest.raises(PhonePilotError):
        v.save("MAIN", "x", codes[:9])
    assert RecoveryVault(tmp_path / "OneDrive" / "v").check_location()
