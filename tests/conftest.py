from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from phonepilot.security.approvals import ApprovalRequest, ApprovalResponse, HumanRequest, HumanResponse
from phonepilot.security.secrets import SecretVault
from phonepilot.testing.fake_device import FakeDevice, demo_screens
from phonepilot.testing.harness import Stack, build_stack

CANARY_PASSWORD = "Canary#PW-7f3a91c2"
CANARY_EMAIL = "canary.user.7f3a@example.com"
CANARY_API = "sk-canary-0123456789abcdef"


@pytest.fixture
def vault() -> SecretVault:
    return SecretVault.from_mapping(
        {
            "GOOGLE_MAIN_EMAIL": CANARY_EMAIL,
            "GOOGLE_MAIN_PASSWORD": CANARY_PASSWORD,
            "PROFILE_MAIN_FIRST_NAME": "길동",
            "UPBIT_MAIN_SECRET_KEY": CANARY_API,
        },
        {
            "GOOGLE_MAIN_EMAIL": {"kind": "username", "allowed_packages": ["com.google.android.gms"]},
            "GOOGLE_MAIN_PASSWORD": {"kind": "password", "allowed_packages": ["com.google.android.gms"]},
            "PROFILE_MAIN_FIRST_NAME": {"kind": "profile", "allowed_packages": ["com.google.android.gms", "com.google.android.youtube"]},
            "UPBIT_MAIN_SECRET_KEY": {"kind": "api_key", "allowed_hosts": ["api.upbit.com"]},
        },
    )


@pytest.fixture
def device() -> FakeDevice:
    return FakeDevice(demo_screens(), "home")


@pytest.fixture
def stack(device: FakeDevice, vault: SecretVault, tmp_path: Path) -> Stack:
    s = build_stack(device, vault, state_dir=tmp_path / "state", vault_dir=tmp_path / "vault")
    s.tools.config.settle_timeout_s = 0.5
    return s


class Approver:
    """Scriptable stand-in for the GUI approval dialog."""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []
        self.humans: list[HumanRequest] = []
        self.answer: Callable[[ApprovalRequest], ApprovalResponse] = lambda r: ApprovalResponse(False)
        self.human_answer: Callable[[HumanRequest], HumanResponse] = lambda r: HumanResponse("continued")

    async def __call__(self, req: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(req)
        return self.answer(req)

    async def human(self, req: HumanRequest) -> HumanResponse:
        self.humans.append(req)
        return self.human_answer(req)

    def approve_all(self) -> None:
        def ok(req: ApprovalRequest) -> ApprovalResponse:
            typed = req.expected_amount if req.expected_amount else "승인"
            return ApprovalResponse(True, typed_confirmation=typed)

        self.answer = ok


@pytest.fixture
def approver(stack: Stack) -> Approver:
    a = Approver()
    stack.approvals.approval_handler = a
    stack.approvals.human_handler = a.human
    return a
