"""Release check / self-update (update.py) and pre-filled problem reports (issue_report.py)."""

import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from phonepilot.errors import PhonePilotError
from phonepilot.issue_report import MAX_URL, build_report, issue_url, scrub
from phonepilot.update import UpdateState, download, fetch_latest, is_newer, parse_release

INSTALLER = b"MZ fake installer bytes" * 1000
SHA = hashlib.sha256(INSTALLER).hexdigest()


def release_json(tag="v0.2.0", with_sha=True, **kw):
    assets = [{"name": f"PhonePilot-{tag[1:]}-setup.exe", "browser_download_url": "https://dl/setup.exe", "size": len(INSTALLER)}]
    if with_sha:
        assets.append({"name": f"PhonePilot-{tag[1:]}-setup.exe.sha256", "browser_download_url": "https://dl/setup.exe.sha256"})
    return {"tag_name": tag, "body": "- 자동 재연결", "html_url": "https://github.com/x/y/releases/tag/" + tag, "assets": assets, **kw}


def client_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_version_compare():
    assert is_newer("v0.2.0", "0.1.0")
    assert is_newer("0.10.0", "0.9.9")  # numeric, not string order
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("v0.2.0rc1", "0.1.0")  # pre-releases are not offered
    assert not is_newer("garbage", "0.1.0")


def test_parse_release_needs_installer_asset():
    rel = parse_release(release_json())
    assert rel is not None and rel.version == "0.2.0" and rel.checksum_url.endswith(".sha256")
    assert parse_release({**release_json(), "assets": []}) is None
    assert parse_release(release_json(draft=True)) is None


async def test_fetch_latest_handles_no_release_yet():
    async with client_for(lambda r: httpx.Response(404, json={"message": "Not Found"})) as c:
        assert await fetch_latest("x/y", c) is None
    async with client_for(lambda r: httpx.Response(200, json=release_json())) as c:
        rel = await fetch_latest("x/y", c)
        assert rel is not None and rel.tag == "v0.2.0"
    async with client_for(lambda r: httpx.Response(500)) as c:
        with pytest.raises(PhonePilotError) as e:
            await fetch_latest("x/y", c)
        assert e.value.code == "UPDATE_CHECK_FAILED"


def downloads(sha_text):
    def route(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith(".sha256"):
            return httpx.Response(200, text=sha_text)
        return httpx.Response(200, content=INSTALLER, headers={"content-length": str(len(INSTALLER))})

    return route


async def test_download_verifies_checksum(tmp_path):
    rel = parse_release(release_json())
    seen = []
    async with client_for(downloads(f"{SHA}  PhonePilot-0.2.0-setup.exe\n")) as c:
        path = await download(rel, tmp_path, lambda done, total: seen.append((done, total)), client=c)
    assert path.read_bytes() == INSTALLER and seen[-1] == (len(INSTALLER), len(INSTALLER))

    async with client_for(downloads("0" * 64)) as c:
        with pytest.raises(PhonePilotError) as e:
            await download(rel, tmp_path / "bad", client=c)
    assert e.value.code == "UPDATE_CHECKSUM_MISMATCH"
    assert not (tmp_path / "bad" / rel.installer_name).exists()  # tampered file is not left behind


async def test_download_refuses_release_without_checksum(tmp_path):
    rel = parse_release(release_json(with_sha=False))
    async with client_for(downloads("")) as c:
        with pytest.raises(PhonePilotError) as e:
            await download(rel, tmp_path, client=c)
    assert e.value.code == "UPDATE_CHECKSUM_MISMATCH"


def test_update_state_throttle(tmp_path):
    st = UpdateState(tmp_path / "s.json")
    assert st.due(24)
    st.last_check, st.skipped = 1000.0, "0.2.0"
    st.save()
    again = UpdateState(tmp_path / "s.json")
    assert again.skipped == "0.2.0"
    assert not again.due(24, now=1000.0 + 3600) and again.due(24, now=1000.0 + 25 * 3600)


def test_report_is_scrubbed(monkeypatch):
    monkeypatch.setattr("os.path.expanduser", lambda p: r"C:\Users\someone")
    text = scrub(r"C:\Users\someone\x key=sk-ant-api03-abcDEF123 ghp_" + "a" * 36 + " Bearer abcdefghijklmnop mail me@ex.com")
    assert "someone" not in text and "abcDEF123" not in text and "a" * 36 not in text
    assert "abcdefghijklmnop" not in text and "me@ex.com" not in text


def test_claude_desktop_registration_merges(tmp_path):
    import json

    from phonepilot.claude_setup import register_desktop

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
    register_desktop(r"C:\PP\phonepilot-cli.exe", ["mcp-stdio"], cfg)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["phonepilot"] == {"command": r"C:\PP\phonepilot-cli.exe", "args": ["mcp-stdio"]}
    assert (tmp_path / "claude_desktop_config.json.bak").exists()

    cfg.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError):
        register_desktop("x", [], cfg)  # never overwrite a file we cannot read
    assert cfg.read_text(encoding="utf-8") == "{ broken"


def test_set_env_value_keeps_other_lines(tmp_path):
    from phonepilot.envfile import set_env_value

    env = tmp_path / ".env"
    env.write_text("# comment\nGOOGLE_MAIN_EMAIL=a@b.c\nANTHROPIC_API_KEY=__set_me__\n", encoding="utf-8")
    set_env_value(env, "ANTHROPIC_API_KEY", "sk-ant-xyz")
    set_env_value(env, "NEW_KEY", "1")
    assert env.read_text(encoding="utf-8") == "# comment\nGOOGLE_MAIN_EMAIL=a@b.c\nANTHROPIC_API_KEY=sk-ant-xyz\nNEW_KEY=1\n"
    with pytest.raises(ValueError):
        set_env_value(env, "BAD", "two\nlines")


def test_publish_release_creates_and_uploads(tmp_path):
    import importlib.util
    import json
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("publish_release", Path(__file__).parents[2] / "scripts" / "publish_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    setup = tmp_path / "PhonePilot-0.2.0-setup.exe"
    setup.write_bytes(b"MZ")
    sha = tmp_path / "PhonePilot-0.2.0-setup.exe.sha256"
    sha.write_text("abc  PhonePilot-0.2.0-setup.exe\n")
    calls = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append((r.method, r.url.host, r.url.path, r.url.params.get("name")))
        assert r.headers["authorization"] == "Bearer t0ken"
        if r.method == "GET":
            return httpx.Response(404)
        if r.url.path.endswith("/releases"):
            assert json.loads(r.content)["tag_name"] == "v0.2.0"
            return httpx.Response(201, json={"upload_url": "https://uploads.github.com/repos/o/r/releases/7/assets{?name,label}", "html_url": "https://github.com/o/r/releases/tag/v0.2.0"})
        return httpx.Response(201, json={})

    url = mod.publish("o/r", "0.2.0", [setup, sha], "notes", "t0ken", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert url.endswith("/v0.2.0")
    assert [c[3] for c in calls if c[1] == "uploads.github.com"] == [setup.name, sha.name]


def test_cli_default_command_keeps_global_flags(monkeypatch):
    from phonepilot import __main__ as cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_gui", lambda a: seen.update(cmd=a.cmd, debug=a.debug, demo=a.demo) or 0)
    monkeypatch.setattr(cli, "_setup_logging", lambda debug: None)
    assert cli.main(["--debug", "--demo"]) == 0
    assert seen == {"cmd": "gui", "debug": True, "demo": True}


def test_issue_url_stays_under_github_limit():
    title, body = build_report("DEVICE_OFFLINE: lost", device="samsung SM-G996N", log_lines=[f"line {i} " + "x" * 300 for i in range(40)])
    url = issue_url("x/y-releases", title, body)
    assert len(url) <= MAX_URL
    q = parse_qs(urlparse(url).query)
    assert q["labels"] == ["bug"] and q["title"][0].startswith("[오류] DEVICE_OFFLINE")
    assert "PhonePilot" in q["body"][0] and "잘렸습니다" in q["body"][0]
