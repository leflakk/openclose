"""Model registry — tracks available models and their capabilities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelInfo:
    """Metadata for a model."""

    id: str
    name: str = ""
    context_window: int = 128_000
    max_output_tokens: int = 4_096
    supports_tools: bool = True
    supports_streaming: bool = True


class ModelRegistry:
    """Registry of known models."""

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}

    def register(self, model: ModelInfo) -> None:
        """Register a model."""
        self._models[model.id] = model

    def get(self, model_id: str) -> ModelInfo | None:
        """Get model info by ID."""
        return self._models.get(model_id)

    def get_or_default(self, model_id: str) -> ModelInfo:
        """Get model info, or create a default entry if unknown."""
        if model_id in self._models:
            return self._models[model_id]
        return ModelInfo(id=model_id, name=model_id)

    def list_models(self) -> list[ModelInfo]:
        """List all registered models."""
        return list(self._models.values())


# Global registry with some common defaults
_registry = ModelRegistry()


def get_model_registry() -> ModelRegistry:
    """Get the global model registry."""
    return _registry
