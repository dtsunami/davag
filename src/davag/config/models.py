"""Model seed registry and capability metadata.

Like davai/src/config/models.js, this table is a curated seed (especially for pricing
and video capability), not a closed allowlist: any unrecognized model ID synthesizes
an unverified ModelSpec rather than failing validation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    label: str
    context: int
    max_output: int
    vision: bool = True
    video: bool = False
    effort: bool = True
    thinking: bool = True
    input_price: float | None = None   # USD per 1M input tokens
    output_price: float | None = None  # USD per 1M output tokens
    unverified: bool = False


MODELS: tuple[ModelSpec, ...] = (
    # --- Anthropic ---
    ModelSpec(
        id="claude-opus-5-5",
        provider="anthropic",
        label="Claude Opus 5.5",
        context=1_000_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=None,
        output_price=None,
    ),
    ModelSpec(
        id="claude-opus-5",
        provider="anthropic",
        label="Claude Opus 5",
        context=1_000_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=5.0,
        output_price=25.0,
    ),
    ModelSpec(
        id="claude-sonnet-5",
        provider="anthropic",
        label="Claude Sonnet 5",
        context=1_000_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=2.0,
        output_price=10.0,
    ),
    ModelSpec(
        id="claude-sonnet-5-5",
        provider="anthropic",
        label="Claude Sonnet 5.5",
        context=1_000_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=2.0,
        output_price=10.0,
    ),
    ModelSpec(
        id="claude-haiku-5-5",
        provider="anthropic",
        label="Claude Haiku 5.5",
        context=200_000,
        max_output=64_000,
        vision=True,
        video=False,
        effort=False,
        thinking=False,
        input_price=1.0,
        output_price=5.0,
    ),
    ModelSpec(
        id="claude-haiku-4-5",
        provider="anthropic",
        label="Claude Haiku 4.5",
        context=200_000,
        max_output=64_000,
        vision=True,
        video=False,
        effort=False,
        thinking=False,
        input_price=1.0,
        output_price=5.0,
    ),
    # --- Google Gemini (native video + audio + Files API) ---
    ModelSpec(
        id="gemini-4-carbon",
        provider="gemini",
        label="Gemini 4 Carbon",
        context=1_000_000,
        max_output=65_536,
        vision=True,
        video=True,
        effort=True,
        thinking=True,
        input_price=1.25,
        output_price=10.0,
    ),
    ModelSpec(
        id="gemini-3.8-flash",
        provider="gemini",
        label="Gemini 3.8 Flash",
        context=1_000_000,
        max_output=64_000,
        vision=True,
        video=True,
        effort=False,
        thinking=True,
        input_price=0.30,
        output_price=2.50,
    ),
    ModelSpec(
        id="gemini-4-argon",
        provider="gemini",
        label="Gemini 4 Argon",
        context=1_000_000,
        max_output=64_000,
        vision=True,
        video=True,
        effort=False,
        thinking=True,
        input_price=None,
        output_price=None,
    ),
    ModelSpec(
        id="gemini-2.5-flash",
        provider="gemini",
        label="Gemini 2.5 Flash (Legacy)",
        context=1_000_000,
        max_output=64_000,
        vision=True,
        video=True,
        effort=False,
        thinking=True,
        input_price=0.30,
        output_price=2.50,
    ),
    # --- OpenAI ---
    ModelSpec(
        id="gpt-5.1",
        provider="openai",
        label="GPT-5.1",
        context=400_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=1.25,
        output_price=10.0,
    ),
    ModelSpec(
        id="gpt-5-mini",
        provider="openai",
        label="GPT-5 mini",
        context=400_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=0.25,
        output_price=2.0,
    ),
    # --- Grok (OpenAI-compatible) ---
    ModelSpec(
        id="grok-4",
        provider="grok",
        label="Grok 4",
        context=256_000,
        max_output=64_000,
        vision=True,
        video=False,
        effort=False,
        thinking=False,
        input_price=3.0,
        output_price=15.0,
    ),
    # --- OpenRouter seeds ---
    ModelSpec(
        id="anthropic/claude-opus-5",
        provider="openrouter",
        label="Claude Opus 5 (OpenRouter)",
        context=1_000_000,
        max_output=128_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=5.0,
        output_price=25.0,
    ),
    ModelSpec(
        id="anthropic/claude-sonnet-4.5",
        provider="openrouter",
        label="Claude Sonnet 4.5 (OpenRouter)",
        context=1_000_000,
        max_output=64_000,
        vision=True,
        video=False,
        effort=True,
        thinking=True,
        input_price=3.0,
        output_price=15.0,
    ),
    ModelSpec(
        id="google/gemini-2.5-pro",
        provider="openrouter",
        label="Gemini 2.5 Pro (OpenRouter)",
        context=1_000_000,
        max_output=64_000,
        vision=True,
        video=False,  # OpenRouter Chat Completions wire shape cannot carry native video parts
        effort=True,
        thinking=True,
        input_price=1.25,
        output_price=10.0,
    ),
)

DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-4-carbon",
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-5.1",
    "grok": "grok-4",
    "openrouter": "anthropic/claude-opus-5",
}


def resolve_model(provider: str, model_id: str | None = None) -> ModelSpec:
    prov = provider.strip().lower()
    want = (model_id or "").strip() or DEFAULT_MODELS.get(prov)
    if not want:
        raise ValueError(f'Unknown provider "{provider}"')

    for spec in MODELS:
        if spec.provider == prov and spec.id == want:
            return spec

    # Synthesize an unverified spec so newly-released models work on day zero.
    is_gemini = prov == "gemini" and want.startswith("gemini-")
    is_haiku = prov == "anthropic" and "haiku" in want
    return ModelSpec(
        id=want,
        provider=prov,
        label=want,
        context=1_000_000 if prov in ("anthropic", "gemini") else 200_000,
        max_output=64_000,
        vision=True,
        video=is_gemini,
        effort=not is_haiku and prov != "gemini",
        thinking=not is_haiku,
        input_price=None,
        output_price=None,
        unverified=True,
    )


def with_catalog_overrides(spec: ModelSpec, **kwargs: object) -> ModelSpec:
    return replace(spec, **kwargs)  # type: ignore[arg-type]