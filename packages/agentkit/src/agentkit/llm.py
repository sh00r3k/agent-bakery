"""LLM access for all agents through one OpenAI-compatible seam.

Chat goes to the external gateway (``llm_base_url``); embeddings go to the local
Ollama (``embed_base_url``, free). Every call is metered (tokens + USD estimate)
and a per-request USD ceiling is enforced so a runaway prompt cannot burn budget.

Why OpenAI-compatible and not a vendor SDK: web-ext-pipeline already speaks
OpenAI-compatible (gateway/Ollama/vast.ai) and every agent can be pointed at
the same gateway. One seam, swappable backend, no lock-in. Agents that genuinely
need the Anthropic SDK can install the ``anthropic`` extra and use it directly.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from agentkit.observability import get_logger

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

    from agentkit.config import BaseAgentSettings

log = get_logger("agentkit.llm")

#: A pydantic model subclass — the schema ``complete_json`` validates against.
ModelT = TypeVar("ModelT", bound=BaseModel)


# USD per 1M tokens (input, output). Operator-maintained estimates only — the
# gateway is the billing source of truth. Extend as models are added; unknown
# models fall back to ZERO_PRICE and are metered as $0 with a warning (so cost
# caps never silently block, but you still see the gap in logs).
#
# These are seeded from the gateway.example.com (LiteLLM) model registry and are
# deliberately overridable WITHOUT a code edit: set the ``AGENTKIT_PRICES`` env
# var to a JSON object ``{"model": [in_per_mtok, out_per_mtok], ...}`` and a
# deployment can pin its real gateway prices. Overrides are merged over the
# defaults (so unset models keep the seeded estimate).
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet": (3.0, 15.0),
    "gpt-5": (1.25, 10.0),
    "gemini-pro": (1.25, 5.0),
    "deepseek-chat": (0.3, 1.1),
    "deepseek-reasoner": (0.55, 2.2),
    "qwen3.5-flash": (0.1, 0.4),
    "qwen3.5-plus": (0.4, 1.2),
    "glm-5.1": (0.6, 2.2),
    "kimi-k2.6": (0.6, 2.5),
    "minimax-m3": (0.3, 1.2),
    "grok-4.3": (3.0, 15.0),
}
ZERO_PRICE = (0.0, 0.0)
# Sentinel price for unpriced models when failing CLOSED: high enough that any
# non-trivial call blows past a sane USD ceiling, so an unknown model can never
# slip an unguarded 1M-token request past the cost cap. $1000/Mtok dwarfs every
# real gateway price while staying a finite, loggable number.
UNPRICED_SENTINEL = (1000.0, 1000.0)


def _load_prices() -> dict[str, tuple[float, float]]:
    """Default price table merged with any ``AGENTKIT_PRICES`` JSON override.

    A malformed override is logged and ignored so a bad env var never breaks a
    run (the seeded estimates still apply).
    """
    prices = dict(_DEFAULT_PRICES)
    raw = os.getenv("AGENTKIT_PRICES")
    if not raw:
        return prices
    try:
        override = json.loads(raw)
        for model, pair in override.items():
            in_price, out_price = pair
            prices[str(model)] = (float(in_price), float(out_price))
    except Exception as exc:
        log.warning("llm.bad_prices_override", error=str(exc))
    return prices


# Process-wide price table (env override applied once at import).
PRICES: dict[str, tuple[float, float]] = _load_prices()


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    # Spend attributed per model id, so the dashboard can stack daily cost by
    # model. The accumulating client usage merges these; a single call's usage
    # carries exactly one entry ``{model: cost_usd}``.
    by_model: dict[str, float] = field(default_factory=dict)

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd
        for model, usd in other.by_model.items():
            self.by_model[model] = self.by_model.get(model, 0.0) + usd


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    allow_unpriced: bool = False,
) -> float:
    """Estimate a call's USD cost from the price table.

    Fails CLOSED on an unpriced model: by default an unknown model is billed at
    :data:`UNPRICED_SENTINEL` so the per-request USD ceiling still triggers and a
    runaway 1M-token call cannot slip through unguarded. Set ``allow_unpriced``
    to opt back into the old permissive behavior (meter unpriced models as $0).
    """
    if model in PRICES:
        in_price, out_price = PRICES[model]
    elif allow_unpriced:
        log.warning("llm.unknown_model_metered_zero", model=model)
        in_price, out_price = ZERO_PRICE
    else:
        log.warning("llm.unknown_model_price_failclosed", model=model)
        in_price, out_price = UNPRICED_SENTINEL
    return (prompt_tokens * in_price + completion_tokens * out_price) / 1_000_000


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """Cheap len-based heuristic for the input token count (no tokenizer dep).

    ~4 chars/token is the rough OpenAI rule of thumb; we round up and add a
    small per-message overhead for role/formatting framing. Best-effort: the
    post-call meter uses the gateway's real token counts as the backstop.
    """
    chars = sum(len(m.get("role", "")) + len(m.get("content", "")) for m in messages)
    return chars // 4 + 4 * len(messages)


def estimate_max_cost(
    model: str, prompt_tokens: int, max_tokens: int, *, allow_unpriced: bool = False
) -> float:
    """Worst-case pre-flight cost: all prompt tokens billed + a full max_tokens
    completion, at the model's configured price."""
    return estimate_cost(model, prompt_tokens, max_tokens, allow_unpriced=allow_unpriced)


class JobCostCapExceeded(RuntimeError):
    """Raised when cumulative job spend exceeds the configured per-job cap (BR-007)."""


class CostLimitExceeded(RuntimeError):
    """Raised when a single LLM call's estimated cost exceeds the configured ceiling."""


class JSONValidationError(RuntimeError):
    """Raised when :meth:`LLMClient.complete_json` cannot get schema-valid JSON.

    Signals that every attempt (the initial call + ``max_retries`` corrective
    re-prompts) returned output that was non-JSON or failed pydantic validation.
    Subclasses :class:`RuntimeError` (not pydantic's ``ValidationError``) so a
    caller can catch the *exhausted-retries* outcome distinctly from a single raw
    validation failure, mirroring how :class:`CostLimitExceeded` flags the
    cost-ceiling outcome.
    """


def _is_breaker_failure(exc: BaseException) -> bool:
    """True only for genuine transport/gateway failures that should advance the
    breaker — connection errors, timeouts, and 5xx responses.

    Client-side faults (HTTP 4xx: bad request / auth / not-found / rate-limit /
    unprocessable, etc.) are NOT counted: a single tenant's malformed input must
    never trip the shared per-process breaker for every other tenant. The
    cost-ceiling path raises :class:`CostLimitExceeded` outside the gateway call,
    so it never reaches here, but we treat it as non-counting defensively.
    """
    if isinstance(exc, CostLimitExceeded):
        return False
    try:  # openai is always installed (top-level import), but stay defensive.
        import openai
    except Exception:  # pragma: no cover - openai is a hard dep
        return True
    # Transport-level failures: no HTTP response came back at all.
    if isinstance(exc, openai.APIConnectionError | openai.APITimeoutError):
        return True
    # An HTTP response with a status: count 5xx (server/gateway), ignore 4xx.
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code >= 500
    if isinstance(exc, openai.APIStatusError):
        # APIStatusError always carries a status_code; reached only if the attr
        # was non-int. Be conservative and count it as a gateway failure.
        return True
    # Unknown/unexpected error type -> count it (fail safe toward protecting the
    # gateway), but a recognised 4xx above has already been excluded.
    return True


class CircuitOpenError(RuntimeError):
    """Raised when the LLM circuit breaker is OPEN — the seam is failing fast.

    Signals that recent consecutive calls failed and the breaker is in its
    cooldown window, so this call was refused without touching the gateway. It
    will half-open (allow one trial) once the cooldown elapses.
    """


@dataclass
class _CircuitBreaker:
    """Minimal in-process breaker around the LLM seam (stdlib only).

    CLOSED normally; after ``threshold`` consecutive failures it trips OPEN for
    ``cooldown_s`` and :meth:`check` fails fast. Once the cooldown elapses the
    next call is allowed as a half-open trial: success closes the breaker and
    resets the count, another failure re-opens it for a fresh cooldown.
    ``threshold <= 0`` disables it (always closed). Uses ``time.monotonic`` so a
    wall-clock jump can't keep it stuck open or trip it early.
    """

    threshold: int
    cooldown_s: float
    _failures: int = 0
    _opened_at: float | None = None

    def check(self) -> None:
        """Fail fast if the breaker is OPEN and still inside its cooldown."""
        if self.threshold <= 0 or self._opened_at is None:
            return
        if time.monotonic() - self._opened_at < self.cooldown_s:
            raise CircuitOpenError(
                f"LLM circuit open: {self._failures} consecutive failures, "
                f"cooling down for {self.cooldown_s:.0f}s"
            )
        # Cooldown elapsed -> allow a single half-open trial.
        log.info("llm.breaker_half_open")

    def record_success(self) -> None:
        if self.threshold <= 0:
            return
        if self._opened_at is not None or self._failures:
            log.info("llm.breaker_closed")
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        if self.threshold <= 0:
            return
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()
            log.error("llm.breaker_open", failures=self._failures)


@dataclass
class ToolCall:
    """One function/tool call the model emitted (US-020).

    ``arguments`` is the raw JSON string exactly as the model produced it; the
    caller parses/validates it against the tool's schema (CR-2).
    """

    id: str
    name: str
    arguments: str


@dataclass
class ToolTurn:
    """The result of one tool-calling turn — what ``complete()`` throws away.

    ``content`` is the assistant text (often ``None`` when the model chose to
    call tools); ``tool_calls`` is the (possibly empty) list of requested calls;
    ``finish_reason`` is the gateway's stop reason (``"tool_calls"``/``"stop"``).
    """

    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str


def _estimate_tokens_loose(messages: list[dict[str, Any]]) -> int:
    """Len-based prompt-token estimate tolerant of tool messages.

    Like :func:`estimate_prompt_tokens` but coerces a ``None`` content (normal on
    an assistant tool-call turn) to ``""`` so the pre-flight guard never crashes
    on the tool-calling message shape. Best-effort; the post-call meter is the
    real backstop. Tool *schema* tokens are not counted here.
    """
    chars = sum(len(str(m.get("role", ""))) + len(str(m.get("content") or "")) for m in messages)
    return chars // 4 + 4 * len(messages)


def _json_substring(text: str) -> str:
    """Slice the JSON-object substring out of a (possibly fenced) LLM response.

    Strips a ```` ```json ```` / ```` ``` ```` code fence (``json`` tag matched
    case-insensitively), then returns the first-``{`` to last-``}`` span so
    surrounding prose is tolerated. Returns ``""`` when no object is present; the
    caller feeds the result to :func:`json.loads`, so a missing/invalid object
    raises :class:`json.JSONDecodeError` (the retry trigger) rather than silently
    parsing to ``{}`` — this is why it is distinct from
    :func:`agentkit.prompts.extract_json`, which swallows failures to ``{}``.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start : end + 1]


@dataclass
class LLMClient:
    """Thin async wrapper around two OpenAI-compatible endpoints + cost meter.

    Construct via :meth:`from_settings`. Accumulates usage across calls in
    ``self.usage`` so an agent can attach total spend to its result/trace.
    """

    chat: AsyncOpenAI
    embed: AsyncOpenAI
    model: str
    embed_model: str
    max_tokens: int = 2048
    max_cost_usd: float = 0.10
    # Per-job cumulative cost cap (BR-007). 0 = disabled (no cap).
    job_cost_cap_usd: float = 0.0
    usage: Usage = field(default_factory=Usage)
    # Cost meter: when False (default) an unpriced model fails CLOSED via the
    # sentinel price so the ceiling still bites; True restores metering-as-zero.
    allow_unpriced_models: bool = False
    # In-process breaker around the LLM seam; default-disabled (threshold 0) so
    # directly-constructed test clients keep the old fail-on-every-call shape.
    breaker: _CircuitBreaker = field(
        default_factory=lambda: _CircuitBreaker(threshold=0, cooldown_s=30.0)
    )

    @classmethod
    def from_settings(cls, settings: BaseAgentSettings) -> LLMClient:
        async_openai = AsyncOpenAI
        timeout = httpx.Timeout(settings.llm_timeout_s)
        return cls(
            chat=async_openai(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key or "noauth",
                timeout=timeout,
                max_retries=settings.llm_max_retries,
            ),
            embed=async_openai(
                base_url=settings.embed_base_url,
                api_key=settings.embed_api_key or "ollama",
                timeout=timeout,
                max_retries=settings.llm_max_retries,
            ),
            model=settings.llm_model,
            embed_model=settings.embed_model,
            max_tokens=settings.llm_max_tokens,
            max_cost_usd=settings.llm_max_cost_usd,
            job_cost_cap_usd=settings.llm_job_cost_cap_usd,
            allow_unpriced_models=settings.allow_unpriced_models,
            breaker=_CircuitBreaker(
                threshold=settings.llm_breaker_threshold,
                cooldown_s=settings.llm_breaker_cooldown_s,
            ),
        )

    async def _call_gateway(self, **create_kwargs: Any) -> Any:
        """Run one ``chat.completions.create`` behind the circuit breaker.

        Checks the breaker first (fail fast with :class:`CircuitOpenError` when
        OPEN), then records success/failure so consecutive gateway errors trip
        it, so every chat path is breaker-guarded.
        Also checks the per-job cumulative cost cap (BR-007) before each call.
        """
        self.breaker.check()
        if self.job_cost_cap_usd > 0 and self.usage.cost_usd >= self.job_cost_cap_usd:
            raise JobCostCapExceeded(
                f"job cost cap exceeded: ${self.usage.cost_usd:.4f} >= "
                f"${self.job_cost_cap_usd:.4f} cap (BR-007)"
            )
        try:
            resp = await self.chat.chat.completions.create(**create_kwargs)
        except Exception as exc:
            # Only transport/5xx failures advance the breaker; a client-side 4xx
            # (bad request / unprocessable input) from one tenant must not trip
            # the shared per-process breaker for everyone (see _is_breaker_failure).
            if _is_breaker_failure(exc):
                self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return resp

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> tuple[str, Usage]:
        """One chat completion. Returns (text, usage). Enforces the cost ceiling.

        A pre-flight guard estimates worst-case cost from a len-based input
        token count plus the output cap and refuses BEFORE sending if it exceeds
        ``max_cost_usd`` — so a runaway prompt cannot burn budget. The post-call
        check below stays as the actual-spend backstop (gateway token counts).
        """
        mdl = model or self.model
        out_cap = max_tokens or self.max_tokens

        # Pre-flight (best-effort): worst-case projection vs the ceiling. An
        # unpriced model is projected at the sentinel price (fail CLOSED) unless
        # ``allow_unpriced_models`` opts back into metering it as $0.
        projected = estimate_max_cost(
            mdl,
            estimate_prompt_tokens(messages),
            out_cap,
            allow_unpriced=self.allow_unpriced_models,
        )
        if projected > self.max_cost_usd:
            log.error(
                "llm.cost_preflight_exceeded",
                model=mdl,
                projected_usd=round(projected, 4),
                ceiling=self.max_cost_usd,
            )
            raise CostLimitExceeded(f"pre-flight {projected:.4f} USD > ceiling {self.max_cost_usd}")

        resp = await self._call_gateway(
            model=mdl,
            messages=cast("list[ChatCompletionMessageParam]", messages),
            max_tokens=out_cap,
            temperature=temperature,
            **kwargs,
        )
        u = resp.usage
        usage = Usage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            completion_tokens=getattr(u, "completion_tokens", 0),
            cost_usd=estimate_cost(
                mdl,
                getattr(u, "prompt_tokens", 0),
                getattr(u, "completion_tokens", 0),
                allow_unpriced=self.allow_unpriced_models,
            ),
        )
        if usage.cost_usd > self.max_cost_usd:
            log.error(
                "llm.cost_exceeded",
                model=mdl,
                cost_usd=round(usage.cost_usd, 4),
                ceiling=self.max_cost_usd,
            )
            raise CostLimitExceeded(f"{usage.cost_usd:.4f} USD > ceiling {self.max_cost_usd}")
        usage.by_model = {mdl: usage.cost_usd}
        self.usage.add(usage)
        text = resp.choices[0].message.content or ""
        log.info(
            "llm.complete",
            model=mdl,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=round(usage.cost_usd, 5),
        )
        return text, usage

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> tuple[ToolTurn, Usage]:
        """One tool-calling chat turn. Returns (ToolTurn, usage).

        Identical cost discipline to :meth:`complete` — the same pre-flight
        worst-case guard and post-call backstop against ``max_cost_usd`` (BR-006,
        SAFE-3) — but it surfaces ``message.tool_calls`` and ``finish_reason``
        that :meth:`complete` discards, so a ReAct loop can act on them (US-020).
        ``complete`` is left untouched; this method is purely additive (CR-3).
        """
        mdl = model or self.model
        out_cap = max_tokens or self.max_tokens

        projected = estimate_max_cost(
            mdl,
            _estimate_tokens_loose(messages),
            out_cap,
            allow_unpriced=self.allow_unpriced_models,
        )
        if projected > self.max_cost_usd:
            log.error(
                "llm.cost_preflight_exceeded",
                model=mdl,
                projected_usd=round(projected, 4),
                ceiling=self.max_cost_usd,
            )
            raise CostLimitExceeded(f"pre-flight {projected:.4f} USD > ceiling {self.max_cost_usd}")

        resp = await self._call_gateway(
            model=mdl,
            messages=cast("list[ChatCompletionMessageParam]", messages),
            max_tokens=out_cap,
            temperature=temperature,
            tools=cast("Any", tools),
            tool_choice=cast("Any", tool_choice),
            **kwargs,
        )
        u = resp.usage
        usage = Usage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            completion_tokens=getattr(u, "completion_tokens", 0),
            cost_usd=estimate_cost(
                mdl,
                getattr(u, "prompt_tokens", 0),
                getattr(u, "completion_tokens", 0),
                allow_unpriced=self.allow_unpriced_models,
            ),
        )
        if usage.cost_usd > self.max_cost_usd:
            log.error(
                "llm.cost_exceeded",
                model=mdl,
                cost_usd=round(usage.cost_usd, 4),
                ceiling=self.max_cost_usd,
            )
            raise CostLimitExceeded(f"{usage.cost_usd:.4f} USD > ceiling {self.max_cost_usd}")
        usage.by_model = {mdl: usage.cost_usd}
        self.usage.add(usage)

        choice = resp.choices[0]
        message = choice.message
        raw_calls = getattr(message, "tool_calls", None) or []
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments or "",
            )
            for tc in raw_calls
        ]
        turn = ToolTurn(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "",
        )
        log.info(
            "llm.complete_with_tools",
            model=mdl,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=round(usage.cost_usd, 5),
            tool_calls=len(tool_calls),
            finish_reason=turn.finish_reason,
        )
        return turn, usage

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: type[ModelT],
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        max_retries: int = 2,
        **kwargs: Any,
    ) -> tuple[ModelT, Usage]:
        """One chat completion validated into ``schema`` with bounded corrective retry.

        Delegates each attempt to :meth:`complete`, so the cost ceiling, circuit
        breaker, ``self.usage`` accumulation and ``llm.complete`` logging all apply
        unchanged. Total attempts = ``max_retries + 1``. On a non-JSON /
        :class:`json.JSONDecodeError` / pydantic ``ValidationError`` response the
        failed assistant turn plus a corrective user message (embedding
        ``schema.model_json_schema()``) are appended and the model is re-asked.

        :class:`CostLimitExceeded` and :class:`CircuitOpenError` raised by the inner
        :meth:`complete` PROPAGATE — only JSON/validation failures retry. After the
        last attempt fails, raises :class:`JSONValidationError`. Returns the
        validated model plus the Usage accumulated across all attempts.
        """
        mdl = model or self.model
        convo: list[dict[str, str]] = list(messages)
        total = Usage()
        last_err = ""

        for attempt in range(max_retries + 1):
            text, usage = await self.complete(
                convo,
                model=mdl,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            total.add(usage)
            try:
                obj = schema.model_validate(json.loads(_json_substring(text)))
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_err = str(exc)
                log.warning(
                    "llm.json_retry",
                    model=mdl,
                    attempt=attempt + 1,
                    max_attempts=max_retries + 1,
                    error=last_err,
                )
                corrective = (
                    "Your previous response was not valid against the required "
                    "schema. Return ONLY a JSON object matching this JSON Schema, "
                    "with no prose or code fences:\n"
                    f"{json.dumps(schema.model_json_schema())}"
                )
                convo = [
                    *convo,
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": corrective},
                ]
                continue
            log.info(
                "llm.complete_json",
                model=mdl,
                attempts=attempt + 1,
                prompt_tokens=total.prompt_tokens,
                completion_tokens=total.completion_tokens,
                cost_usd=round(total.cost_usd, 5),
            )
            return obj, total

        log.error("llm.json_invalid", model=mdl, attempts=max_retries + 1, error=last_err)
        raise JSONValidationError(
            f"no schema-valid JSON after {max_retries + 1} attempts: {last_err}"
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed via local Ollama (free). Used for RAG / uniqueness / similarity.

        Shares the chat seam's circuit breaker so a sustained embedding outage
        also fails fast instead of stalling every caller on the timeout.
        """
        self.breaker.check()
        try:
            resp = await self.embed.embeddings.create(model=self.embed_model, input=texts)
        except Exception as exc:
            if _is_breaker_failure(exc):
                self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return [d.embedding for d in resp.data]
