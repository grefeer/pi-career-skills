"""DeepSeek model factory + env-based API key provider.

Port of migration plan section 8 (DeepSeek integration) and the source
``_thinking_extra_body`` helper (``backend/app/services/agent_plugins/
infrastructure/models.py``), restricted to the DeepSeek provider only.

The module returns standalone ``pi_ai.types.Model`` instances; it does not
register anything in the global pi-ai registry. The run controller owns
wiring.
"""

from __future__ import annotations

import os
from typing import Any

from pi_ai.types import Model

from .errors import MODEL_API_KEY_MISSING, CareerToolError

# ---------------------------------------------------------------------------
# Constants (plan section 8)
# ---------------------------------------------------------------------------

DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY_ENV: str = "DEEPSEEK_API_KEY"
DEFAULT_DEEPSEEK_CONTEXT_WINDOW: int = 64_000
DEFAULT_DEEPSEEK_MAX_TOKENS: int = 8_192


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thinking_extra_body(model_id: str) -> dict[str, Any]:
    """Return ``extra_body`` that disables hidden reasoning for v4 models.

    Keyed on the *model id* (lower-cased, prefix match), never on the base
    URL. Mirrors the source helper restricted to the DeepSeek branch; the
    qwen branches are not ported because this module is deepseek-only.
    """

    lowered = model_id.lower()
    if lowered.startswith("deepseek-v4"):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_deepseek_model(model_id: str) -> Model:
    """Build a ``Model`` descriptor for a DeepSeek chat model.

    Follows migration plan section 8 exactly:
    ``api="openai-completions"``, ``provider="deepseek"``, base URL with
    ``/v1`` suffix, ``reasoning=False``, text-only input, 64k context
    window, 8192 max tokens, and ``sampling_params`` carrying
    ``temperature=0`` plus the v4 thinking-disable flag when applicable.
    """

    sampling_params: dict[str, Any] = {"temperature": 0}
    sampling_params.update(_thinking_extra_body(model_id))

    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="deepseek",
        base_url=DEEPSEEK_BASE_URL,
        reasoning=False,
        input=["text"],
        context_window=DEFAULT_DEEPSEEK_CONTEXT_WINDOW,
        max_tokens=DEFAULT_DEEPSEEK_MAX_TOKENS,
        sampling_params=sampling_params,
    )


def get_deepseek_api_key() -> str | None:
    """Return the DeepSeek API key from the environment, or ``None``.

    Reads **only** the ``DEEPSEEK_API_KEY`` variable. Never logs the key
    or its length; never stores it on the model.
    """

    return os.environ.get(DEEPSEEK_API_KEY_ENV) or None


def resolve_api_key(provider: str) -> str | None:
    """Resolve the API key for *provider*.

    - ``"deepseek"`` — returns the env-var key, or raises
      :class:`CareerToolError` with code ``MODEL_API_KEY_MISSING``.
    - anything else (e.g. ``"faux"``) — returns ``None`` (no key needed).
    """

    if provider == "deepseek":
        key = get_deepseek_api_key()
        if not key:
            raise CareerToolError(
                MODEL_API_KEY_MISSING,
                "missing DEEPSEEK_API_KEY",
            )
        return key
    return None


__all__ = [
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY_ENV",
    "DEFAULT_DEEPSEEK_CONTEXT_WINDOW",
    "DEFAULT_DEEPSEEK_MAX_TOKENS",
    "Model",
    "CareerToolError",
    "MODEL_API_KEY_MISSING",
    "_thinking_extra_body",
    "create_deepseek_model",
    "get_deepseek_api_key",
    "resolve_api_key",
]
