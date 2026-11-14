from __future__ import annotations

import logging
from dataclasses import dataclass

import ollama

log = logging.getLogger(__name__)


class OllamaClientError(RuntimeError):
    """Raised when the local Ollama backend cannot produce a response."""


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:1.5b"
    timeout_seconds: float = 90.0
    temperature: float = 0.2
    max_output_tokens: int = 256
    # How long Ollama keeps the model in memory after a request. Its own
    # default (5 minutes) means a pause mid-demo makes the next answer wait
    # for the model to load again.
    keep_alive: str = "30m"


class OllamaClient:
    """Thin async wrapper around the Ollama Python client."""

    def __init__(self, config: OllamaConfig) -> None:
        self.config = config
        self._client = ollama.AsyncClient(
            host=config.base_url,
            timeout=config.timeout_seconds,
        )
        # The model actually used: the base model, until use_custom_model()
        # has built the domain model.
        self.model = config.model
        self._system_baked_in = False

    async def use_custom_model(self, name: str, system: str) -> bool:
        """Build `name` from the base model with `system` baked in as its
        system prompt and the decoding settings fixed - the same as
        `ollama create` with a Modelfile:

            FROM qwen2.5:1.5b
            SYSTEM <Tapri's persona and house rules, from prompts.py>
            PARAMETER temperature 0.2

        Creating it again is cheap (no new weights, only a small layer), so
        it is rebuilt at every start and always matches prompts.py. On
        failure the base model keeps being used, with the system prompt sent
        per request."""
        try:
            await self._client.create(
                model=name,
                from_=self.config.model,
                system=system,
                parameters={"temperature": self.config.temperature, "num_predict": self.config.max_output_tokens},
            )
        except Exception as exc:
            log.warning("could not build the domain model %s (%s) - using %s", name, exc, self.config.model)
            return False
        self.model, self._system_baked_in = name, True
        log.info("domain model %s built from %s", name, self.config.model)
        return True

    async def generate(self, prompt: str, *, system: str) -> str:
        """Generate one non-streaming response from Ollama. `system` is sent
        only when the model doesn't already have it baked in."""
        try:
            response = await self._client.generate(
                model=self.model,
                system=None if self._system_baked_in else system,
                prompt=prompt,
                stream=False,
                options={
                    "temperature": self.config.temperature,
                    "num_predict": self.config.max_output_tokens,
                },
                keep_alive=self.config.keep_alive,
            )
        except ollama.ResponseError as exc:
            raise OllamaClientError(
                f"Ollama returned an error "
                f"(status={exc.status_code}): {exc.error}"
            ) from exc
        except Exception as exc:
            raise OllamaClientError(
                f"Could not reach Ollama at {self.config.base_url}: {exc}"
            ) from exc

        answer = getattr(response, "response", None)

        if answer is None and isinstance(response, dict):
            answer = response.get("response")

        if not isinstance(answer, str) or not answer.strip():
            raise OllamaClientError("Ollama returned an empty response.")

        return answer.strip()

    async def close(self) -> None:
        """Close the underlying async HTTP client when supported."""
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()