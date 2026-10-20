"""Judge abstraction (spec §5).

Wire types follow TypeSafe's System One contract (`noul` / `choice` / `score`);
hosts that speak `bool` map it at their adapter. One logical judgment is one
physical POST: no application retries, absolute deadline, caller-owned client.
Failures raise `JudgeError`; the pipeline decides what may fail open.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import os
import re
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence

import httpx

TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
_USAGE_KEYS = ("input_tokens", "output_tokens")
_MODEL_NAME = re.compile(r"jev[-A-Za-z0-9_.]{0,75}")

JSON = Any
QuestionType = Literal["noul", "choice", "score"]


@dataclass(frozen=True)
class Question:
    type: QuestionType
    instructions: JSON
    criteria: JSON = None

    def to_wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            body["criteria"] = self.criteria
        return body


def noul(instructions: JSON, *, true: JSON = None, false: JSON = None) -> Question:
    criteria = None
    if true is not None or false is not None:
        criteria = {k: v for k, v in (("true", true), ("false", false)) if v is not None}
    return Question("noul", instructions, criteria)


def choice(instructions: JSON, options: Mapping[str, JSON]) -> Question:
    if len(options) < 2:
        raise ValueError("choice needs at least two options")
    return Question("choice", instructions, dict(options))


def score(instructions: JSON, levels: Sequence[JSON]) -> Question:
    if not 2 <= len(levels) <= 10:
        raise ValueError("score needs 2..10 levels")
    return Question("score", instructions, list(levels))


@dataclass(frozen=True)
class Answer:
    type: QuestionType
    noul: Optional[float] = None
    choice: Optional[str] = None
    probabilities: Optional[dict[str, float]] = None
    confidence: Optional[float] = None
    score: Optional[float] = None

    @property
    def p(self) -> float:
        """Probability for noul answers; raises for other types."""
        if self.type != "noul" or self.noul is None:
            raise TypeError("not a noul answer")
        return self.noul


@dataclass
class JudgeResponse:
    answers: dict[str, Answer]
    backend: str
    model: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    request_id: Optional[str] = None
    posts: int = 1


class JudgeError(Exception):
    """reason ∈ unavailable | authentication | http_error | malformed_response | timeout | network | cancelled"""

    def __init__(self, reason: str, detail: str = "", *, posts: int | None = None) -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        if posts is not None:
            self.posts = posts


class Judge(Protocol):
    backend: str

    async def judge(self, state: JSON, questions: Mapping[str, Question], *,
                    deadline: Optional[float] = None) -> JudgeResponse: ...


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON constant")


class NoJudge:
    backend = "none"

    async def judge(self, state: JSON, questions: Mapping[str, Question], *,
                    deadline: Optional[float] = None) -> JudgeResponse:
        raise JudgeError("unavailable", "no judge backend configured")


def _finite_unit(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("non-numeric probability")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("probability out of range")
    return value


def parse_answer(raw: Any, question: Question) -> Answer:
    """Validate one wire answer against its question. Raises ValueError."""
    if not isinstance(raw, dict) or raw.get("type") != question.type:
        raise ValueError("answer type mismatch")
    if question.type == "noul":
        return Answer("noul", noul=_finite_unit(raw.get("noul")))
    if question.type == "choice":
        options = set(question.criteria or {})
        probabilities = raw.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != options:
            raise ValueError("choice probabilities do not match options")
        probs = {k: _finite_unit(v) for k, v in probabilities.items()}
        selected = raw.get("choice")
        if selected not in options:
            raise ValueError("choice outside options")
        return Answer("choice", choice=selected, probabilities=probs,
                      confidence=_finite_unit(raw.get("confidence")))
    levels = len(question.criteria or [])
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != {str(i) for i in range(levels)}:
        raise ValueError("score probabilities do not match levels")
    probs = {k: _finite_unit(v) for k, v in probabilities.items()}
    value = raw.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid score")
    return Answer("score", score=float(value), probabilities=probs,
                  confidence=_finite_unit(raw.get("confidence")))


def parse_response(payload: Any, questions: Mapping[str, Question], backend: str) -> JudgeResponse:
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise ValueError("missing answers")
    answers_raw = payload["answers"]
    if set(answers_raw) != set(questions):
        raise ValueError("answer ids do not match questions")
    answers = {qid: parse_answer(answers_raw[qid], question) for qid, question in questions.items()}
    model = payload.get("model")
    if not (isinstance(model, str) and _MODEL_NAME.fullmatch(model)):
        model = None
    usage_raw = payload.get("usage")
    usage = {}
    if isinstance(usage_raw, dict):
        for key in _USAGE_KEYS:
            value = usage_raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12:
                usage[key] = value
    return JudgeResponse(answers=answers, backend=backend, model=model, usage=usage)


class ReplayJudge:
    """Deterministic judge for tests and contract replays.

    `script` is a sequence of wire-shaped `answers` maps consumed in call order,
    or a callable receiving (state, questions) and returning one.
    """
    backend = "replay"

    def __init__(self, script: Sequence[Mapping[str, Any]] | Callable[[JSON, Mapping[str, Question]], Mapping[str, Any]],
                 *, model: str = "jev-replay") -> None:
        self._script = script
        self._model = model
        self.calls: list[tuple[JSON, dict[str, Question]]] = []

    async def judge(self, state: JSON, questions: Mapping[str, Question], *,
                    deadline: Optional[float] = None) -> JudgeResponse:
        index = len(self.calls)
        self.calls.append((state, dict(questions)))
        if callable(self._script):
            answers = self._script(state, questions)
        else:
            if index >= len(self._script):
                raise JudgeError("unavailable", "replay script exhausted")
            answers = self._script[index]
        try:
            return parse_response({"answers": answers, "model": self._model,
                                   "usage": {"input_tokens": 0, "output_tokens": 0}},
                                  questions, self.backend)
        except ValueError as error:
            raise JudgeError("malformed_response", str(error)) from error


class TypeSafeJudge:
    """Direct System One client. Single POST, no redirects, no retries."""
    backend = "typesafe"

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL,
                 client: Optional[httpx.AsyncClient] = None,
                 endpoint: str = TYPESAFE_ENDPOINT, soft_budget: float = 3.0) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key required")
        self._key = api_key
        self._model = model
        self._client = client
        self._endpoint = endpoint
        self.soft_budget = soft_budget

    async def judge(self, state: JSON, questions: Mapping[str, Question], *,
                    deadline: Optional[float] = None) -> JudgeResponse:
        loop = asyncio.get_running_loop()
        now = loop.time()
        limit = now + self.soft_budget
        if deadline is not None:
            limit = min(limit, deadline)
        remaining = limit - now
        if remaining <= 0:
            raise JudgeError("timeout", "no budget left before request", posts=0)
        body = {"model": self._model, "state": state,
                "questions": {qid: q.to_wire() for qid, q in questions.items()}}
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}

        posts = 0

        async def post(active: httpx.AsyncClient) -> httpx.Response:
            nonlocal posts
            posts += 1
            return await active.post(self._endpoint, json=body, headers=headers,
                                     timeout=remaining, follow_redirects=False)

        try:
            async with asyncio.timeout_at(limit):
                if self._client is None:
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as owned:
                        response = await post(owned)
                else:
                    response = await post(self._client)
        except asyncio.CancelledError:
            raise JudgeError("cancelled", posts=posts) from None
        except (TimeoutError, httpx.TimeoutException):
            raise JudgeError("timeout", posts=posts) from None
        except httpx.HTTPError as error:
            raise JudgeError("network", type(error).__name__, posts=posts) from None

        if response.status_code in (401, 403):
            raise JudgeError("authentication")
        if not 200 <= response.status_code < 300:
            raise JudgeError("http_error", str(response.status_code))
        try:
            payload = json.loads(response.content, parse_constant=_reject_constant)
            parsed = parse_response(payload, questions, self.backend)
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise JudgeError("malformed_response", str(error)) from None
        parsed.request_id = response.headers.get("x-request-id")
        return parsed


def resolve_judge(*, host_judge: Optional[Judge] = None, env: Mapping[str, str] | None = None,
                  client: Optional[httpx.AsyncClient] = None) -> Judge:
    """Spec §5 order: host judge → TypeSafe key → none. Default-on (CR-9)."""
    if host_judge is not None:
        return host_judge
    env = os.environ if env is None else env
    key = env.get("TYPESAFE_API_KEY", "")
    if key.strip():
        return TypeSafeJudge(key, model=env.get("WRR_JEV_MODEL", DEFAULT_MODEL), client=client)
    return NoJudge()
