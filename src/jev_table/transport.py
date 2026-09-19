"""The only module that talks to a Jev-compatible endpoint.

``Transport`` is a narrow protocol so tests (and future backends) can swap the
HTTP layer without touching the engine. Retries and 429/529 backoff are the
official SDK's job; we pin the SDK minor version because the API is early
access.
"""

from __future__ import annotations

from typing import Any, Protocol

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeError

DEFAULT_MODEL = "jev-latest"
DEFAULT_BASE_URL = "https://api.typesafe.ai"


class TransportError(Exception):
    """One row's evaluation failed, after the SDK exhausted its retries."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Transport(Protocol):
    async def evaluate(
        self, state: Any, questions: dict[str, dict[str, Any]], model: str
    ) -> dict[str, Any]:
        """Return {"model", "answers", "usage"} for one state."""
        ...

    async def aclose(self) -> None: ...


class TypeSafeTransport:
    """Live transport backed by the official TypeSafe SDK."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 5,
        timeout: float = 60.0,
    ) -> None:
        self._client = AsyncTypeSafeClient(
            api_key=api_key,
            base_url=base_url,
            retry=RetryPolicy(max_retries=max_retries),
            timeout=timeout,
        )

    async def evaluate(
        self, state: Any, questions: dict[str, dict[str, Any]], model: str
    ) -> dict[str, Any]:
        try:
            response = await self._client.system_one(state=state, questions=questions, model=model)
        except TypeSafeError as exc:
            raise TransportError(str(exc), status=_status_of(exc)) from exc
        return {
            "model": response.model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "answers": {qid: answer.model_dump() for qid, answer in response.answers.items()},
        }

    async def aclose(self) -> None:
        await self._client.aclose()


def _status_of(exc: TypeSafeError) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    try:
        status = exc.response.status_code  # type: ignore[attr-defined]
    except Exception:
        return None
    return status if isinstance(status, int) else None
