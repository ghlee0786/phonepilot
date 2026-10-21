"""TC-OBS-01..04: UI tree normalization, password masking, SoM, stability."""

import numpy as np

from phonepilot.observe import uitree, vision
from phonepilot.testing.fake_device import FakeDevice, demo_screens

LOGIN_XML = FakeDevice(demo_screens(), "google_signin")._xml()


def test_normalize_login_screen_tc_obs_01():
    tree = uitree.parse_xml(LOGIN_XML)
    elements, blocks, truncated = uitree.normalize(tree, 1080, 2400)
    roles = [e.role for e in elements]
    assert roles.count("text_field") == 2
    assert roles.count("button") == 3
    assert [e.id for e in elements] == list(range(1, len(elements) + 1))
    # reading order: email field above password field above buttons
    assert elements[0].label == "이메일 또는 휴대전화"
    assert any(b["text"] == "로그인" for b in blocks)
    assert truncated == 0


def test_password_field_masked_tc_obs_02():
    dev = FakeDevice(demo_screens(), "google_signin")
    dev.values[("google_signin", "com.google.android.gms:id/password")] = "hunter2hunter2"
    xml = dev._xml()
    assert "hunter2" not in xml  # the device itself reports dots for password fields
    xml = xml.replace("•" * 14, "hunter2hunter2")  # simulate a leaky app
    elements, _, _ = uitree.normalize(uitree.parse_xml(xml), 1080, 2400)
    pw = next(e for e in elements if e.password)
    assert pw.text == uitree.PASSWORD_MASK
    assert "hunter2" not in uitree.serialize_elements(elements)


def test_selectors():
    elements, _, _ = uitree.normalize(uitree.parse_xml(LOGIN_XML), 1080, 2400)
    assert uitree.find(elements, {"text": "다음"})[0].role == "button"
    assert uitree.find(elements, {"text_any": ["Next", "다음"]})
    assert uitree.find(elements, {"resource_id": "com.google.android.gms:id/email"})[0].role == "text_field"
    assert uitree.find(elements, {"role": "button", "index": 1})[0].label == "계정 만들기"
    assert uitree.find(elements, {"text_contains": "잊으셨"})
    near = uitree.find(elements, {"role": "button", "near": {"text": "계정 만들기"}})
    assert near[0].label == "다음"
    assert uitree.element_at(elements, 800, 1950).label == "다음"


def test_truncation():
    nodes = "".join(
        f'<node text="item {i}" resource-id="" class="android.widget.Button" package="p" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,{i*10}][100,{i*10+9}]" />'
        for i in range(200)
    )
    xml = f"<?xml version='1.0'?><hierarchy rotation='0'>{nodes}</hierarchy>"
    elements, _, truncated = uitree.normalize(uitree.parse_xml(xml), 1080, 2400, max_elements=150)
    assert len(elements) == 150 and truncated == 50


def test_som_render_tc_obs_03():
    img = vision.to_image(np.full((1200, 540, 3), 255, np.uint8))
    out = vision.render_som(img, [(i, (10, i * 200, 500, i * 200 + 150)) for i in range(1, 11)], (1080, 2400))
    assert out.size == img.size
    assert np.asarray(out).min() < 255  # something drawn


def test_stability_tc_obs_04():
    t = vision.StabilityTracker(needed=3)
    a = np.zeros((100, 50, 3), np.uint8)
    b = np.full((100, 50, 3), 255, np.uint8)
    b[:50] = 0
    assert not t.feed(a)
    assert not t.feed(b)  # changed
    assert not t.feed(b)
    assert t.feed(b)  # 3 identical in a row


def test_mask_boxes_and_black_detection():
    img = vision.to_image(np.full((240, 108, 3), 200, np.uint8))
    masked = vision.mask_boxes(img, [(0, 0, 540, 1200)], (1080, 2400))
    arr = np.asarray(masked)
    assert arr[10, 10].tolist() == [40, 40, 40]
    assert vision.looks_black(np.zeros((50, 50, 3), np.uint8))
    assert not vision.looks_black(arr)
