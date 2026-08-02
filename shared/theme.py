"""
Design system partagé — thématisation MLB / NPB / KBO.

Usage dans chaque app (après `st.set_page_config`) :

    from shared.theme import apply_theme, render_page_header, afficher_cartes_matchs
    apply_theme("mlb")  # ou "npb" / "kbo"
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Mapping, Optional

import streamlit as st

# ---------------------------------------------------------------------------
# Palettes par ligue (couleurs officielles / esthétique de référence)
# ---------------------------------------------------------------------------
LEAGUE_THEMES: dict[str, dict[str, str]] = {
    "mlb": {
        "label": "MLB",
        "full_name": "Major League Baseball",
        "primary": "#0C2340",       # Bleu marine
        "secondary": "#C8102E",     # Rouge baseball
        "accent": "#FFFFFF",
        "on_primary": "#FFFFFF",
        "bg": "#F3F6FA",
        "card_bg": "#FFFFFF",
        "chip_bg": "#EEF3F8",
        "text": "#152033",
        "muted": "#5B6B7C",
        "border": "rgba(12, 35, 64, 0.12)",
        "success": "#1F7A4D",
        "danger": "#C8102E",
        "glow": "rgba(200, 16, 46, 0.12)",
        "header_grad": "linear-gradient(135deg, #0C2340 0%, #163A5F 58%, #C8102E 130%)",
    },
    "npb": {
        "label": "NPB",
        "full_name": "Nippon Professional Baseball",
        "primary": "#111111",       # Noir
        "secondary": "#E60012",     # Rouge vif japonais
        "accent": "#FFFFFF",
        "on_primary": "#FFFFFF",
        "bg": "#F7F7F8",
        "card_bg": "#FFFFFF",
        "chip_bg": "#F2F2F3",
        "text": "#141414",
        "muted": "#5C5C5C",
        "border": "rgba(17, 17, 17, 0.12)",
        "success": "#1B7A4E",
        "danger": "#E60012",
        "glow": "rgba(230, 0, 18, 0.10)",
        "header_grad": "linear-gradient(135deg, #111111 0%, #2A2A2A 55%, #E60012 125%)",
    },
    "kbo": {
        "label": "KBO",
        "full_name": "Korea Baseball Organization",
        "primary": "#0033A0",       # Bleu roi
        "secondary": "#B0B7C3",     # Argent
        "accent": "#FFFFFF",
        "on_primary": "#FFFFFF",
        "bg": "#F2F5FB",
        "card_bg": "#FFFFFF",
        "chip_bg": "#EDF1F8",
        "text": "#142033",
        "muted": "#5A6A7D",
        "border": "rgba(0, 51, 160, 0.13)",
        "success": "#1F7A4D",
        "danger": "#E31C23",        # Touche de rouge dynamique
        "glow": "rgba(0, 51, 160, 0.12)",
        "header_grad": "linear-gradient(135deg, #0033A0 0%, #1A4BB8 55%, #8E97A8 120%)",
    },
}


def _css_path() -> Path:
    return Path(__file__).resolve().parent / "styles.css"


def _escape(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return html.escape(text) if text else "—"


def _inject_css_variables(theme: Mapping[str, str]) -> str:
    return f"""
:root {{
  --ps-primary: {theme['primary']};
  --ps-secondary: {theme['secondary']};
  --ps-accent: {theme['accent']};
  --ps-on-primary: {theme['on_primary']};
  --ps-bg: {theme['bg']};
  --ps-card-bg: {theme['card_bg']};
  --ps-chip-bg: {theme['chip_bg']};
  --ps-text: {theme['text']};
  --ps-muted: {theme['muted']};
  --ps-border: {theme['border']};
  --ps-success: {theme['success']};
  --ps-danger: {theme['danger']};
  --ps-glow: {theme['glow']};
  --ps-header-grad: {theme['header_grad']};
}}
"""


def apply_theme(league: str) -> dict[str, str]:
    """
    Injecte le CSS partagé + les variables de la ligue.
    À appeler une seule fois après `st.set_page_config`.
    Retourne le dict de thème actif.
    """
    key = (league or "mlb").strip().lower()
    if key not in LEAGUE_THEMES:
        key = "mlb"
    theme = LEAGUE_THEMES[key]
    st.session_state["ps_league"] = key
    st.session_state["ps_theme"] = theme

    css_file = _css_path()
    base_css = css_file.read_text(encoding="utf-8") if css_file.exists() else ""
    variables = _inject_css_variables(theme)

    st.markdown(
        f"<style>\n{variables}\n{base_css}\n</style>",
        unsafe_allow_html=True,
    )
    return theme


def get_active_theme() -> dict[str, str]:
    theme = st.session_state.get("ps_theme")
    if isinstance(theme, dict):
        return theme
    league = st.session_state.get("ps_league", "mlb")
    return LEAGUE_THEMES.get(league, LEAGUE_THEMES["mlb"])


def render_page_header(title: str, tagline: str, league: Optional[str] = None) -> None:
    """En-tête de page aux couleurs de la ligue (remplace st.title brut)."""
    key = (league or st.session_state.get("ps_league") or "mlb").lower()
    theme = LEAGUE_THEMES.get(key, LEAGUE_THEMES["mlb"])
    st.markdown(
        f"""
        <div class="ps-hero">
          <p class="ps-hero__eyebrow">{_escape(theme['full_name'])}</p>
          <h1 class="ps-hero__brand">{_escape(title)}</h1>
          <p class="ps-hero__tagline">{_escape(tagline)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_title(title: str, subtitle: Optional[str] = None) -> None:
    """Titre de section avec barre d'accent ligue."""
    st.markdown(
        f"""
        <div class="ps-section-title">
          <span class="ps-section-title__bar"></span>
          <h2 class="ps-section-title__text">{_escape(title)}</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if subtitle:
        st.markdown(
            f'<p class="ps-section-sub">{_escape(subtitle)}</p>',
            unsafe_allow_html=True,
        )


def badge_html(label: str, kind: str = "status") -> str:
    """
    Badge Win / Loss / pending / value / avoid / neutral / status.
    `kind` accepte aussi les icônes ✅ ❌ ⏳ issues des colonnes Résultat vs Algo.
    """
    mapping = {
        "✅": ("win", "Win"),
        "❌": ("loss", "Loss"),
        "⏳": ("pending", "Pending"),
        "win": ("win", label or "Win"),
        "loss": ("loss", label or "Loss"),
        "pending": ("pending", label or "Pending"),
        "value": ("value", label or "Value Bet"),
        "avoid": ("avoid", label or "Éviter"),
        "neutral": ("neutral", label or "Juste"),
        "status": ("status", label or "Statut"),
        "evitez": ("avoid", label or "Éviter"),
    }
    kind_key = (kind or "status").strip()
    if kind_key in mapping:
        css_kind, default_label = mapping[kind_key]
        text = label if label and kind_key not in {"✅", "❌", "⏳"} else default_label
        if kind_key in {"✅", "❌", "⏳"} and label and label not in {"✅", "❌", "⏳"}:
            text = label
        elif kind_key in {"✅", "❌", "⏳"}:
            text = f"{kind_key} {default_label}"
    else:
        css_kind, text = "status", label or kind_key
    return f'<span class="ps-badge ps-badge--{css_kind}">{_escape(text)}</span>'


def _badge_from_result_icon(icon: Any) -> str:
    raw = (str(icon).strip() if icon is not None else "") or "⏳"
    if "✅" in raw:
        return badge_html(raw if len(raw) > 1 else "Favori OK", "win")
    if "❌" in raw:
        return badge_html(raw if len(raw) > 1 else "Contre", "loss")
    return badge_html(raw if len(raw) > 1 else "En attente", "pending")


def render_match_card_html(row: Mapping[str, Any]) -> str:
    """HTML d'une carte match (une ligne du DataFrame résumé)."""
    match = _escape(row.get("Match", "Match"))
    statut = _escape(row.get("Statut", "—"))
    score = _escape(row.get("Score", "—"))
    total = _escape(row.get("Total Runs", "—"))
    hrs = _escape(row.get("Home Runs", "—"))
    comparatif = _escape(row.get("Comparatif Prédiction", "—"))
    resultat = row.get("Résultat vs Algo", "⏳")

    return f"""
    <article class="ps-match-card">
      <div class="ps-match-card__top">
        <h3 class="ps-match-card__title">{match}</h3>
        {badge_html(statut, "status")}
      </div>
      <p class="ps-match-card__score">{score}</p>
      <div class="ps-match-card__meta">
        <div class="ps-match-card__meta-item">
          <span class="ps-match-card__meta-label">Total Runs</span>
          <span class="ps-match-card__meta-value">{total}</span>
        </div>
        <div class="ps-match-card__meta-item">
          <span class="ps-match-card__meta-label">Home Runs</span>
          <span class="ps-match-card__meta-value">{hrs}</span>
        </div>
        <div class="ps-match-card__meta-item" style="grid-column: 1 / -1;">
          <span class="ps-match-card__meta-label">Comparatif Prédiction</span>
          <span class="ps-match-card__meta-value">{comparatif}</span>
        </div>
      </div>
      <div class="ps-match-card__footer">
        <span style="color:var(--ps-muted);font-size:0.8rem;font-weight:600;">Résultat vs Algo</span>
        {_badge_from_result_icon(resultat)}
      </div>
    </article>
    """


def afficher_cartes_matchs(df, *, show_table_fallback: bool = True, column_config=None) -> None:
    """
    Affiche chaque match du DataFrame résumé sous forme de carte.
    Conserve optionnellement une vue tableau dans un expander (tri / export).
    """
    if df is None or getattr(df, "empty", True):
        return

    cards = "\n".join(render_match_card_html(row) for _, row in df.iterrows())
    st.markdown(f'<div class="ps-match-grid">{cards}</div>', unsafe_allow_html=True)

    if show_table_fallback:
        with st.expander("📋 Vue tableau compacte", expanded=False):
            kwargs = {"hide_index": True, "use_container_width": True}
            if column_config is not None:
                kwargs["column_config"] = column_config
            st.dataframe(df, **kwargs)


def render_prediction_match_banner(title: str, subtitle: str = "") -> None:
    """
    Bandeau-carte au-dessus du bloc Prédictions.
    (Streamlit ne permet pas d'encapsuler des widgets dans un vrai <div> HTML :
    on stylise donc l'en-tête + les métriques via CSS global.)
    """
    sub = f'<p class="ps-card__subtitle">{_escape(subtitle)}</p>' if subtitle else ""
    st.markdown(
        f'<div class="ps-card"><h3 class="ps-card__title">{_escape(title)}</h3>{sub}</div>',
        unsafe_allow_html=True,
    )


# Alias conservés pour compatibilité avec d'éventuels appels existants
def render_prediction_card_open(title: str, subtitle: str = "") -> None:
    render_prediction_match_banner(title, subtitle)


def render_card_close() -> None:
    return


def afficher_badge_value_bet(niveau: str, message: str) -> None:
    """Affiche un message Value Bet avec badge coloré (sans changer la logique métier)."""
    if not message:
        return
    kind = {"value": "value", "evitez": "avoid", "juste": "neutral"}.get(niveau, "neutral")
    label = {"value": "Value Bet", "evitez": "Éviter", "juste": "Cote juste"}.get(niveau, "Info")
    st.markdown(
        f'<div class="ps-card" style="padding:0.85rem 1rem;">'
        f'{badge_html(label, kind)}'
        f'<p style="margin:0.55rem 0 0 0;color:var(--ps-text);line-height:1.45;">'
        f'{_escape(message)}</p></div>',
        unsafe_allow_html=True,
    )


def render_footer(league_label: str, date_str: str) -> None:
    st.markdown(
        f'<div class="ps-footer"><strong>{_escape(league_label)}</strong> Analytics · '
        f'Données mises à jour : {_escape(date_str)}</div>',
        unsafe_allow_html=True,
    )


def ensure_shared_on_path(app_file: str) -> None:
    """
    Ajoute au `sys.path` le dossier parent qui contient `shared/`.
    Cherche d'abord à côté de l'app, puis au niveau monorepo (parent).
    """
    import sys

    here = Path(app_file).resolve().parent
    for base in (here, here.parent):
        if (base / "shared" / "theme.py").is_file():
            base_str = str(base)
            if base_str not in sys.path:
                sys.path.insert(0, base_str)
            return
