from dataclasses import dataclass, field

from .base import GamePlugin, PluginState


@dataclass
class StarRatingState:
    images: list[str]
    index: int = 0
    # image filename -> list of ratings (1-5) per player
    votes: dict[str, list[int]] = field(default_factory=dict)


class StarRatingPlugin(GamePlugin):
    """
    Each participant rates the current image 1–5 stars.
    Winner threshold = minimum average score (e.g. 3.5).
    """

    def __init__(self) -> None:
        self._state: StarRatingState | None = None

    # ------------------------------------------------------------------
    # GamePlugin interface
    # ------------------------------------------------------------------

    def start(self, images: list[str]) -> PluginState:
        self._state = StarRatingState(images=images)
        for img in images:
            self._state.votes[img] = []
        return self._snapshot()

    def vote(self, player: str, payload: dict) -> None:
        assert self._state is not None, "Plugin not started"
        rating = int(payload["rating"])
        if not 1 <= rating <= 5:
            raise ValueError(f"Rating must be 1–5, got {rating}")
        current = self._state.images[self._state.index]
        self._state.votes[current].append(rating)

    def next_image(self) -> PluginState:
        assert self._state is not None, "Plugin not started"
        if not self.is_finished():
            self._state.index += 1
        return self._snapshot()

    def get_state(self) -> PluginState:
        assert self._state is not None, "Plugin not started"
        return self._snapshot()

    def get_winners(self, threshold: float) -> list[str]:
        assert self._state is not None, "Plugin not started"
        winners = []
        for img, ratings in self._state.votes.items():
            if not ratings:
                continue
            avg = sum(ratings) / len(ratings)
            if avg >= threshold:
                winners.append(img)
        return winners

    def is_finished(self) -> bool:
        assert self._state is not None, "Plugin not started"
        return self._state.index >= len(self._state.images) - 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _average(self, img: str) -> float:
        ratings = self._state.votes.get(img, [])
        return round(sum(ratings) / len(ratings), 2) if ratings else 0.0

    def _snapshot(self) -> PluginState:
        s = self._state
        current = s.images[s.index] if s.images else None
        ratings = s.votes.get(current, []) if current else []
        return PluginState(
            current_image=current,
            image_index=s.index,
            total_images=len(s.images),
            extra={
                "mode": "star_rating",
                "vote_count": len(ratings),
                "current_avg": self._average(current) if current else 0.0,
                # send per-image averages so host can see a live leaderboard
                "scores": {img: self._average(img) for img in s.images},
            },
        )
