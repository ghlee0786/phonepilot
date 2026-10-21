"""Executable skill schema (PRD 11.2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepKind = Literal["action", "observe", "assert", "branch", "loop", "agent_step", "human", "approval", "secret_input", "call", "capture"]


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str | None = None
    text: str | None = None
    text_any: list[str] | None = None
    text_gone: str | None = None
    element: dict[str, Any] | None = None
    timeout_ms: int = 10000


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: StepKind = "action"
    description: str = ""
    # action
    action: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    then: Literal["tap"] | None = None
    expect: Expect | None = None
    skip_if: Expect | None = None
    retry: int | None = None
    optional: bool = False
    # assert / branch / loop
    condition: Expect | None = None
    message: str = ""
    goto: str | None = None
    else_goto: str | None = None
    until: Expect | None = None
    max_iter: int = 5
    steps: list[Step] = Field(default_factory=list)
    # agent_step
    goal: str = ""
    success: Expect | None = None
    max_actions: int = 15
    allowed_tools: list[str] | None = None
    # human / approval
    kind: str = "manual_step"
    instructions: str = ""
    title: str = ""
    summary: str = ""
    risk: Literal["low", "medium", "high"] = "medium"
    # secret_input
    ref: str | None = None
    target: dict[str, Any] | None = None
    submit: bool = False
    # call / capture
    skill: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    capture: str | None = None
    account_ref: str | None = None


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "int", "bool", "secret_ref", "list"] = "string"
    kind: str | None = None
    required: bool = False
    default: Any = None
    description: str = ""


class Requires(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_sdk: int = 26
    packages: list[str] = Field(default_factory=list)


class OnError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry: int = 2
    then: Literal["agent_recover", "human", "fail"] = "agent_recover"


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str = "1.0.0"
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    locales: list[str] = Field(default_factory=lambda: ["ko", "en"])
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    requires: Requires = Field(default_factory=Requires)
    sensitive: bool = False
    account_creation: bool = False
    on_error: OnError = Field(default_factory=OnError)
    steps: list[Step]
    final_check: Expect | None = None
    report: list[str] = Field(default_factory=list)
