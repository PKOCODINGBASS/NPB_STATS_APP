"""Design system partagé du monorepo PARIS SPORTIFS (MLB / NPB / KBO)."""

from .theme import (
    LEAGUE_THEMES,
    apply_theme,
    afficher_cartes_matchs,
    afficher_badge_value_bet,
    badge_html,
    render_page_header,
    render_section_title,
    render_match_card_html,
    render_prediction_match_banner,
    render_footer,
)

__all__ = [
    "LEAGUE_THEMES",
    "apply_theme",
    "afficher_cartes_matchs",
    "afficher_badge_value_bet",
    "badge_html",
    "render_page_header",
    "render_section_title",
    "render_match_card_html",
    "render_prediction_match_banner",
    "render_footer",
]
