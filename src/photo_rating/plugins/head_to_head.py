import itertools
import random
from dataclasses import dataclass, field

from .base import GamePlugin, PluginState


@dataclass
class HeadToHeadState:
    images: list[str]
    pairs: list[tuple[str, str]]
    index: int = 0
    # image filename -> win count
    wins: dict[str, int] = field(default_factory=dict)
    # image filename -> number of times it appeared in a pair
    appearances: dict[str, int] = field(default_factory=dict)


class HeadToHeadPlugin(GamePlugin):
    """
    Two images shown side-by-side; participants pick the better one.
    All unique pairs are generated upfront and shuffled.
    Winner threshold = minimum win-rate (wins / appearances), e.g. 0.5.
    """

    def __init__(self) -> None:
        self._state: HeadToHeadState | None = None

    # ------------------------------------------------------------------
    # GamePlugin interface
    # ------------------------------------------------------------------

    def start(self, images: list[str]) -> PluginState:
        pairs = list(itertools.combinations(images, 2))
        random.shuffle(pairs)
        wins = {img: 0 for img in images}
        appearances = {img: 0 for img in images}
        # pre-count appearances from the pair list
        for a, b in pairs:
            appearances[a] += 1
            appearances[b] += 1
        self._state = HeadToHeadState(
            images=images, pairs=pairs, wins=wins, appearances=appearances
        )
        return self._snapshot()

    def vote(self, player: str, payload: dict) -> None:
        assert self._state is not None, "Plugin not started"
        winner = payload["winner"]  # filename of chosen image
        if winner not in self._state.wins:
            raise ValueError(f"Unknown image: {winner}")
        self._state.wins[winner] += 1

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
        for img in self._state.images:
            rate = self._win_rate(img)
            if rate >= threshold:
                winners.append(img)
        return winners

    def is_finished(self) -> bool:
        assert self._state is not None, "Plugin not started"
        return self._state.index >= len(self._state.pairs) - 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _win_rate(self, img: str) -> float:
        s = self._state
        app = s.appearances.get(img, 0)
        return round(s.wins[img] / app, 2) if app else 0.0

    def _snapshot(self) -> PluginState:
        s = self._state
        if not s.pairs:
            return PluginState(current_image=None, image_index=0, total_images=0)
        pair = s.pairs[s.index]
        return PluginState(
            current_image=pair[0],  # first of the pair (second is in extra)
            image_index=s.index,
            total_images=len(s.pairs),
            extra={
                "mode": "head_to_head",
                "pair": list(pair),
                "win_rates": {img: self._win_rate(img) for img in s.images},
            },
        )
