from .head_to_head import HeadToHeadPlugin
from .star_rating import StarRatingPlugin

PLUGIN_REGISTRY: dict[str, type] = {
    "star_rating": StarRatingPlugin,
    "head_to_head": HeadToHeadPlugin,
}
