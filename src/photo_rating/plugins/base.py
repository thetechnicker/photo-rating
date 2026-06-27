from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PluginState:
    """Serialisable snapshot returned to clients via polling."""

    current_image: str | None
    image_index: int
    total_images: int
    extra: dict[str, object] = field(default_factory=dict[str, object])  # mode-specific payload


class GamePlugin(ABC):
    """
    Contract every game mode must fulfil.
    Internal state lives in plain dataclasses.
    All public methods return plain dicts or dataclasses — never Pydantic models,
    so plugins stay independent of the API layer.
    """

    @abstractmethod
    def start(self, images: list[str]) -> PluginState:
        """Initialise plugin with the full image list. Returns initial state."""

    @abstractmethod
    def vote(self, player: str, payload: dict[str, object]) -> None:
        """Record a vote from a player. Payload shape is mode-specific."""

    @abstractmethod
    def next_image(self) -> PluginState:
        """Advance to the next image/pair. Returns updated state."""

    @abstractmethod
    def get_state(self) -> PluginState:
        """Return current state snapshot (used by polling endpoint)."""

    @abstractmethod
    def get_winners(self, threshold: float) -> list[str]:
        """
        Return filenames of images that meet the winning threshold.
        Threshold semantics are mode-specific (avg stars / win-rate).
        """

    @abstractmethod
    def is_finished(self) -> bool:
        """True when all images have been shown."""
