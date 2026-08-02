"""Design system partagé du monorepo PARIS SPORTIFS (MLB / NPB / KBO).

Volontairement minimal : ne pas réimporter theme ici.
Sur Streamlit Cloud, un `__init__.py` qui fait `from .theme import ...`
peut masquer l'erreur réelle ou entrer en conflit avec d'autres paquets
nommés `shared`. Les apps importent `shared.theme` (ou chargent theme.py
par chemin absolu).
"""
