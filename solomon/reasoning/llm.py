"""LLM client for Solomon.

Solomon does its own LLM calls (salience scoring, classification, S1, S2,
audit gate). It uses the same provider config as Hermes — same API keys,
same base URLs — but goes direct, not through Hermes's chat loop. This
keeps the conductor's pre/post_llm_call hooks simple: they handle the
USER's turn, while these calls happen "off to the side" during the
hook handlers.

Provider resolution order:
  1. SOLOMON_LLM_BASE_URL + SOLOMON_LLM_API_KEY env vars (explicit override)
  2. OPENROUTER_API_KEY (default; supports most models)
  3. ANTHROPIC_API_KEY
  4. Whatever Hermes has configured (read via adapter.get_config)

Models we use:
  - SOLOMON_MODEL_FAST: salience, classification, System 1 prediction,
    sleep cycle stress test. Default 'anthropic/claude-sonnet-4'.
  - SOLOMON_MODEL_DEEP: System 2 reasoning, audit gate, sleep cycle surprise
    replay, mentoring questions. Default 'anthropic/claude-opus-4.7'.
  - SOLOMON_MODEL_ONBOARDING: onboarding and ingestion. Always the deepest
    available model — these are one-time, per-tenant calls. Default
    'anthropic/claude-opus-4.7'.

Every call returns plain text. JSON parsing happens at the call site
because each component has its own schema.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("solomon.llm")


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient:
    """One client per Solomon process. Routes to whichever provider is
    configured, defaults to OpenRouter.
    """

    def __init__(self) -> None:
        self.base_url = os.getenv("SOLOMON_LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("SOLOMON_LLM_API_KEY", "")

        # Fall back to OpenRouter if nothing else is set.
        if not self.base_url or not self.api_key:
            or_key = os.getenv("OPENROUTER_API_KEY", "")
            if or_key:
                self.base_url = "https://openrouter.ai/api/v1"
                self.api_key = or_key

        self._model_fast = os.getenv("SOLOMON_MODEL_FAST", "anthropic/claude-sonnet-4")
        self._model_deep = os.getenv("SOLOMON_MODEL_DEEP", "anthropic/claude-opus-4.7")
        self._model_onboarding = os.getenv("SOLOMON_MODEL_ONBOARDING", "anthropic/claude-opus-4.7")

        self._http = httpx.Client(timeout=60.0)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def model_for(self, tier: str) -> str:
        return {
            "fast": self._model_fast,
            "deep": self._model_deep,
            "onboarding": self._model_onboarding,
        }.get(tier, self._model_fast)

    def call(
        self,
        *,
        tier: str = "fast",
        system: str = "",
        user: str = "",
        json_mode: bool = False,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Single shot call. Returns the assistant text, or an empty string
        on error (with a logged warning — calls in the conductor hot path
        should never crash the user's turn).
        """
        if not self.configured:
            logger.warning("Solomon LLM client not configured. Returning empty response.")
            return LLMResponse(text="", model=self.model_for(tier))

        model = self.model_for(tier)
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            r = self._http.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage", {}) or {}
            return LLMResponse(
                text=text,
                model=model,
                tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                tokens_out=int(usage.get("completion_tokens", 0) or 0),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Solomon LLM call (%s) failed: %s", tier, e)
            return LLMResponse(text="", model=model)

    @staticmethod
    def parse_json(text: str) -> Optional[Dict[str, Any]]:
        """Best-effort JSON parse. Models sometimes wrap JSON in ```json fences."""
        if not text:
            return None
        t = text.strip()
        if t.startswith("```"):
            # strip code fences
            lines = t.splitlines()
            t = "\n".join(lines[1:-1]) if len(lines) >= 3 else t.strip("`")
        try:
            return json.loads(t)
        except Exception:  # noqa: BLE001
            # Try to find the first { ... } block.
            start, end = t.find("{"), t.rfind("}")
            if 0 <= start < end:
                try:
                    return json.loads(t[start:end + 1])
                except Exception:  # noqa: BLE001
                    return None
            return None


# Process-wide singleton.
_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
