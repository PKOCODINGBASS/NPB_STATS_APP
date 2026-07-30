"""
Application d'Analyse Statistiques NPB (Nippon Professional Baseball)
===================================================================
Application Streamlit pour analyser les runs, les sluggers récurrents et les tendances W/L
de la ligue japonaise de baseball (NPB - 12 équipes, Central League + Pacific League).

Il n'existe PAS d'équivalent officiel de "MLB StatsAPI" pour la NPB (pas d'API publique
documentée). Les données (calendrier, scores, boxscores joueur par joueur, lanceurs
partants annoncés, stats de lanceur) sont donc récupérées par scraping ciblé de pages
HTML PUBLIQUES du site officiel https://npb.jp (aucune authentification requise), via
`requests` + `BeautifulSoup`. Voir les commentaires de chaque fonction pour le détail
des pages utilisées.

Auteur: Généré via MAMMOUTH AI (adaptation NPB, version scraping npb.jp)
"""

# ============================================================
# 1. IMPORTS - On importe les bibliothèques nécessaires
# ============================================================
import streamlit as st          # Framework pour créer l'interface web
import pandas as pd             # Manipulation de données (tableaux)
import altair as alt            # Graphiques avancés (ligne de moyenne annotée)
import re                       # Extraction des noms/runs et parsing HTML ciblé
import time                     # Délais/backoff entre les appels réseau
import requests                 # Appels HTTP vers npb.jp (scraping)
from bs4 import BeautifulSoup   # Parsing HTML des pages npb.jp
from datetime import datetime   # Gestion des dates
from zoneinfo import ZoneInfo   # Gestion des fuseaux horaires (JST <-> heure française)

# ============================================================
# Fuseaux horaires : les matchs NPB sont annoncés et joués en heure du Japon
# (JST, UTC+9, PAS d'heure d'été au Japon). Un match "du soir" à 18h JST
# correspond à 10h ou 11h du matin en France (selon heure d'été/hiver), et un
# match "de jour" à 13h/14h JST tombe en pleine nuit française (5h-6h du
# matin). Toute la logique "quel est le match d'aujourd'hui ?" doit donc se
# baser sur la date/l'heure AU JAPON, jamais sur la date/l'heure française.
# ============================================================
TZ_JST = ZoneInfo("Asia/Tokyo")
TZ_PARIS = ZoneInfo("Europe/Paris")

# Année NPB courante, basée sur la date du jour AU JAPON (fuseau JST)
ANNEE_COURANTE = datetime.now(TZ_JST).year

# Mois couverts par la saison régulière + Climax Series + Japan Series
# (le site npb.jp publie une page "schedule_MM_detail.html" par mois de mars à novembre)
MOIS_SAISON = list(range(3, 12))

# En-tête HTTP "poli" : on s'identifie clairement, sans se faire passer pour un navigateur
HEADERS_HTTP = {"User-Agent": "Mozilla/5.0 (compatible; NPBStatsApp/1.0; +https://npb.jp)"}

_SESSION = requests.Session()
_SESSION.headers.update(HEADERS_HTTP)


def appeler_avec_retry(fonction, *args, tentatives: int = 3, delai_base: float = 0.5, **kwargs):
    """
    Exécute `fonction(*args, **kwargs)` avec un système de retry + backoff exponentiel.

    Objectif : éviter que le scraping npb.jp fasse "disparaître" silencieusement des
    équipes/joueurs/matchs à cause d'une erreur réseau transitoire ou d'un rejet
    temporaire (timeout, erreur 429/5xx, etc.). Sans cela, un simple `except: continue`
    avalerait l'erreur et sauterait l'équipe/le match sans aucune nouvelle tentative ni
    message - c'est une des causes classiques du bug "certaines équipes ne se mettent
    pas à jour".

    Le délai n'intervient qu'EN CAS D'ÉCHEC (pas avant chaque appel), donc les appels
    réussis (le cas normal) ne sont pas ralentis. Comme la plupart des fonctions qui
    utilisent cet appel sont elles-mêmes mises en cache par Streamlit, ce délai ne
    s'applique de toute façon qu'au premier chargement (cache miss), pas aux reruns.
    """
    derniere_erreur = None
    for tentative in range(1, tentatives + 1):
        try:
            return fonction(*args, **kwargs)
        except Exception as e:
            derniere_erreur = e
            if tentative < tentatives:
                time.sleep(delai_base * (2 ** (tentative - 1)))  # 0.5s, 1s, 2s, ...
    raise derniere_erreur


def _get_soup(url: str, timeout: float = 10.0) -> BeautifulSoup:
    """Télécharge une page npb.jp et retourne son arbre HTML parsé (BeautifulSoup)."""
    reponse = _SESSION.get(url, timeout=timeout)
    reponse.raise_for_status()
    reponse.encoding = reponse.apparent_encoding or "utf-8"
    return BeautifulSoup(reponse.text, "html.parser")


# ============================================================
# 2. CONFIGURATION DE LA PAGE - Paramètres de l'application
# ============================================================
st.set_page_config(
    page_title="Analyse NPB - Runs & Sluggers",
    page_icon="⚾",
    layout="wide"
)

# ============================================================
# 3. LISTE DES ÉQUIPES NPB (12 équipes, codes officiels npb.jp)
# ============================================================
# Les codes (clés) sont ceux utilisés tels quels dans les URLs npb.jp (en minuscule
# côté scraping). On les affiche en MAJUSCULE dans l'interface, comme les
# abréviations MLB de la version précédente ("G - Yomiuri Giants", etc.).
TEAMS_NPB = {
    # -- Central League --
    "G": "Yomiuri Giants",
    "T": "Hanshin Tigers",
    "DB": "Yokohama DeNA BayStars",
    "D": "Chunichi Dragons",
    "C": "Hiroshima Toyo Carp",
    "S": "Tokyo Yakult Swallows",
    # -- Pacific League --
    "H": "Fukuoka SoftBank Hawks",
    "F": "Hokkaido Nippon-Ham Fighters",
    "B": "Orix Buffaloes",
    "E": "Tohoku Rakuten Golden Eagles",
    "L": "Saitama Seibu Lions",
    "M": "Chiba Lotte Marines",
}

# Noms courts japonais utilisés dans le calendrier npb.jp ("team1"/"team2") pour
# retrouver le code équipe (g, t, db, ...) MÊME quand le match n'a pas encore de
# lien vers un boxscore (ce qui est le cas pour tout match futur non encore joué).
NOM_COURT_TO_CODE = {
    "巨人": "g", "阪神": "t", "DeNA": "db", "中日": "d", "広島": "c", "ヤクルト": "s",
    "ソフトバンク": "h", "日本ハム": "f", "オリックス": "b", "楽天": "e", "西武": "l", "ロッテ": "m",
}

# ============================================================
# 4. FONCTIONS DE CHARGEMENT DES DONNÉES (avec mise en cache)
# ============================================================

@st.cache_data
def get_teams_npb(annee: int = None):
    """
    Retourne la liste des 12 équipes NPB. Contrairement à MLB StatsAPI, npb.jp n'expose
    pas d'endpoint listant "les équipes de la saison X" : la liste des 12 franchises est
    stable d'une année sur l'autre (contrairement aux Home Runs/scores, qui eux sont bien
    scrapés en direct plus bas). Le paramètre `annee` est conservé pour la parité
    d'interface avec le reste du code, mais n'est pas utilisé.
    """
    return dict(TEAMS_NPB)


def extraire_abreviation_equipe(nom_equipe: str) -> str:
    """
    Extrait le code NPB depuis une chaîne 'CODE - Nom complet'.
    Exemple: 'G - Yomiuri Giants' -> 'G'
    """
    return nom_equipe.split(' - ')[0].strip()


@st.cache_data(show_spinner=False, ttl=1800)
def charger_calendrier_mensuel(annee: int, mois: int) -> pd.DataFrame:
    """
    Récupère, en UNE SEULE requête, le calendrier ET les résultats de TOUS les matchs
    NPB (12 équipes confondues) pour un mois donné, directement depuis la page
    officielle NPB.jp : https://npb.jp/games/{annee}/schedule_{mois}_detail.html

    Cette page liste pour chaque match : équipe à domicile / à l'extérieur, score,
    stade, heure (heure du Japon - JST), lanceur gagnant/perdant si le match est
    terminé, ainsi qu'un lien vers le boxscore détaillé (utilisé plus bas pour
    récupérer les runs/home runs par joueur).

    Comme cette fonction ne dépend PAS de l'équipe sélectionnée, Streamlit ne fait cet
    appel réseau qu'UNE SEULE FOIS par (année, mois), quel que soit le nombre d'équipes
    consultées ensuite dans la session - un mois entier de calendrier NPB (~36 matchs)
    tient dans une seule page HTML, contre un appel par match sur d'autres sources.
    """
    url = f"https://npb.jp/games/{annee}/schedule_{mois:02d}_detail.html"
    try:
        soup = appeler_avec_retry(_get_soup, url)
    except Exception:
        return pd.DataFrame()

    lignes = []
    for tr in soup.select('tr[id^="date"]'):
        m_date = re.match(r'date(\d{2})(\d{2})', tr.get('id', ''))
        if not m_date:
            continue
        mm, dd = m_date.group(1), m_date.group(2)

        tds = tr.find_all('td', recursive=False)
        if len(tds) < 1:
            continue

        cell_equipes = tds[0]
        div_home = cell_equipes.find('div', class_='team1')
        div_away = cell_equipes.find('div', class_='team2')
        if div_home is None or div_away is None:
            continue
        nom_home = div_home.get_text(strip=True)
        nom_away = div_away.get_text(strip=True)

        # Le code équipe (g, t, db, ...) est déterminé PRIORITAIREMENT via le nom
        # court japonais (fiable même pour un match futur pas encore joué, où le
        # lien vers le boxscore n'existe pas encore dans le HTML), avec le lien
        # "/scores/.../{home}-{away}-{N}/" en repli/complément dès qu'il existe (il
        # nous donne alors aussi l'URL du boxscore détaillé).
        code_home = NOM_COURT_TO_CODE.get(nom_home)
        code_away = NOM_COURT_TO_CODE.get(nom_away)
        box_url = None

        lien = cell_equipes.find('a', href=True)
        if lien is not None:
            m_href = re.search(r'/scores/\d{4}/\d{4}/([a-z]+)-([a-z]+)-\d+/', lien['href'])
            if m_href:
                code_home = code_home or m_href.group(1)
                code_away = code_away or m_href.group(2)
                box_url = "https://npb.jp" + lien['href'] if lien['href'].startswith('/') else lien['href']

        # Les scores (score1/score2) sont cherchés directement dans la cellule,
        # qu'ils soient ou non enveloppés dans un lien <a> (le lien n'existe que
        # pour les matchs déjà joués/en cours) - un match futur affiche "&nbsp;"
        # à la place d'un chiffre, d'où le test `isdigit()`.
        score_home, score_away = None, None
        div_s1 = cell_equipes.find('div', class_='score1')
        div_s2 = cell_equipes.find('div', class_='score2')
        if div_s1 is not None and div_s2 is not None:
            t1, t2 = div_s1.get_text(strip=True), div_s2.get_text(strip=True)
            if t1.isdigit() and t2.isdigit():
                score_home, score_away = int(t1), int(t2)

        lieu, heure_jst = "", ""
        if len(tds) > 1:
            div_place = tds[1].find('div', class_='place')
            div_time = tds[1].find('div', class_='time')
            lieu = div_place.get_text(strip=True) if div_place else ""
            heure_jst = div_time.get_text(strip=True) if div_time else ""

        # La 4e cellule contient soit la décision finale (勝/敗 = gagnant/perdant,
        # une fois le match terminé), soit les partants annoncés la veille
        # (先発 = "starter"), dans l'ordre visuel équipe domicile puis extérieur.
        lanceur_gagnant, lanceur_perdant = "", ""
        lanceur_annonce_home, lanceur_annonce_away = "", ""
        if len(tds) > 3:
            annonces = []
            for div_pit in tds[3].find_all('div', class_='pit'):
                texte = div_pit.get_text(strip=True)
                if texte.startswith('勝'):
                    lanceur_gagnant = texte.split('：', 1)[-1]
                elif texte.startswith('敗'):
                    lanceur_perdant = texte.split('：', 1)[-1]
                elif texte.startswith('先発'):
                    annonces.append(texte.split('：', 1)[-1])
            if len(annonces) >= 1:
                lanceur_annonce_home = annonces[0]
            if len(annonces) >= 2:
                lanceur_annonce_away = annonces[1]

        lignes.append({
            "Date": f"{annee}-{mm}-{dd}",
            "code_home": code_home,
            "code_away": code_away,
            "nom_home": nom_home,
            "nom_away": nom_away,
            "score_home": score_home,
            "score_away": score_away,
            "lieu": lieu,
            "heure_jst": heure_jst,
            "box_url": box_url,
            "lanceur_gagnant": lanceur_gagnant,
            "lanceur_perdant": lanceur_perdant,
            "lanceur_annonce_home": lanceur_annonce_home,
            "lanceur_annonce_away": lanceur_annonce_away,
        })

    return pd.DataFrame(lignes)


@st.cache_data(show_spinner=False, ttl=1800)
def charger_donnees_equipe(annee: int = None, equipe_abbr: str = None) -> pd.DataFrame:
    """
    Charge les données de match TERMINÉS pour une équipe donnée, sur toute la saison,
    en assemblant les calendriers mensuels (mars à novembre) via `charger_calendrier_mensuel`.
    Affiche deux colonnes distinctes: 'Équipe Domicile' et 'Équipe Extérieur' (comme la
    version MLB d'origine).
    """
    if annee is None:
        annee = ANNEE_COURANTE
    if not equipe_abbr:
        return pd.DataFrame()

    code_equipe = equipe_abbr.lower()

    frames = [charger_calendrier_mensuel(annee, mois) for mois in MOIS_SAISON]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()

    df_tout = pd.concat(frames, ignore_index=True)
    masque = (df_tout['code_home'] == code_equipe) | (df_tout['code_away'] == code_equipe)
    df_equipe = df_tout[masque].copy()

    # On ne garde que les matchs dont le score est connu des deux côtés...
    df_equipe = df_equipe.dropna(subset=['score_home', 'score_away'])
    if df_equipe.empty:
        return pd.DataFrame()

    # ...ET qui sont réellement terminés : pour la date du jour (heure du Japon), un
    # match peut être EN COURS avec un score partiel déjà affiché sur la page. On ne
    # le considère "terminé" que s'il a eu lieu un jour STRICTEMENT antérieur à
    # aujourd'hui (JST), ou si une décision (lanceur gagnant/perdant) a déjà été
    # publiée (ce qui n'arrive qu'en fin de match).
    aujourdhui_jst = datetime.now(TZ_JST).strftime('%Y-%m-%d')
    est_termine = (
        (df_equipe['Date'] < aujourdhui_jst)
        | (df_equipe['lanceur_gagnant'] != '')
        | (df_equipe['lanceur_perdant'] != '')
    )
    df_equipe = df_equipe[est_termine]
    if df_equipe.empty:
        return pd.DataFrame()

    try:
        matchs = []
        for _, g in df_equipe.iterrows():
            est_dom = (g['code_home'] == code_equipe)
            nom_home_aff = TEAMS_NPB.get(g['code_home'].upper(), g['nom_home']) if g['code_home'] else g['nom_home']
            nom_away_aff = TEAMS_NPB.get(g['code_away'].upper(), g['nom_away']) if g['code_away'] else g['nom_away']

            if est_dom:
                runs, runs_adverses = int(g['score_home']), int(g['score_away'])
            else:
                runs, runs_adverses = int(g['score_away']), int(g['score_home'])

            if runs > runs_adverses:
                wl = "W"
            elif runs < runs_adverses:
                wl = "L"
            else:
                wl = "T"

            matchs.append({
                "Date": g['Date'],
                "Équipe Domicile": nom_home_aff,
                "Équipe Extérieur": nom_away_aff,
                "R": runs,
                "RA": runs_adverses,
                "W/L": wl,
                "box_url": g['box_url'],
                "Est_Domicile": est_dom,
            })
        df = pd.DataFrame(matchs).sort_values('Date').reset_index(drop=True)
        return df
    except Exception as e:
        st.error(f"Erreur lors du chargement des données pour {equipe_abbr} ({annee}): {e}")
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def get_stats_offensives_match(box_url: str, est_domicile: bool):
    """
    Récupère, via le boxscore npb.jp d'un match (page japonaise détaillée, la seule à
    exposer les runs marqués par batteur), les runs ET les home runs marqués par
    chaque joueur de l'équipe (domicile ou extérieur) lors de ce match.
    Retourne une liste de dicts {'name': str, 'runs': int, 'hr': int}.

    Détail technique npb.jp :
    - Le tableau des batteurs de l'équipe à DOMICILE a pour id HTML "tablefix_b_b"
      ("b" = bottom, l'équipe qui frappe en bas de manche), celui de l'équipe à
      l'EXTÉRIEUR a pour id "tablefix_t_b" ("t" = top).
    - La colonne "得点" (5e colonne) donne directement le nombre de runs marqués par
      le joueur sur ce match.
    - npb.jp n'a PAS de colonne dédiée aux Home Runs par joueur : chaque case
      "manche" affiche le résultat de l'action de jeu (ex: "右越本①" = home run par
      dessus le champ droit). On compte donc, pour chaque joueur, le nombre de cases
      de manche contenant le caractère "本" (本塁打 = home run).
    """
    if not box_url:
        return []
    url = box_url if box_url.endswith('.html') else box_url.rstrip('/') + '/box.html'

    try:
        soup = appeler_avec_retry(_get_soup, url)
    except Exception:
        return []

    table_id = 'tablefix_b_b' if est_domicile else 'tablefix_t_b'
    table = soup.find('table', id=table_id)
    if table is None:
        return []
    tbody = table.find('tbody')
    if tbody is None:
        return []

    stats_par_joueur = {}
    for tr in tbody.find_all('tr', recursive=False):
        tds = tr.find_all('td', recursive=False)
        if len(tds) < 8:
            continue

        lien_joueur = tds[2].find('a')
        if lien_joueur is None:
            continue  # ligne "チーム計" (total équipe), pas un joueur

        nom = lien_joueur.get_text(strip=True)
        if not nom:
            continue

        try:
            runs = int(tds[4].get_text(strip=True) or 0)
        except ValueError:
            runs = 0

        cellules_manches = tds[8:]
        hr = sum(1 for td in cellules_manches if '本' in td.get_text())

        if runs > 0 or hr > 0:
            if nom not in stats_par_joueur:
                stats_par_joueur[nom] = {'runs': 0, 'hr': 0}
            stats_par_joueur[nom]['runs'] += runs
            stats_par_joueur[nom]['hr'] += hr

    return [{'name': nom, 'runs': s['runs'], 'hr': s['hr']} for nom, s in stats_par_joueur.items()]


@st.cache_data(show_spinner=False, ttl=1800)
def get_matchs_avec_scoreurs(annee: int, equipe_abbr: str):
    """
    Enrichit les données de match avec la liste des scoreurs de runs ET de home runs
    par match, et calcule le cumul de runs / home runs marqués par joueur sur toute
    la période chargée.
    Retourne (df_matchs_enrichi, df_meilleurs_scoreurs_runs, df_meilleurs_scoreurs_hr).
    """
    df = charger_donnees_equipe(annee, equipe_abbr)
    if df.empty or 'box_url' not in df.columns:
        return df, pd.DataFrame(), pd.DataFrame()

    df = df.copy()
    colonne_joueurs_runs = []
    colonne_joueurs_hr = []
    colonne_stats_brutes = []
    cumul_runs = {}
    cumul_hr = {}

    for _, ligne in df.iterrows():
        stats_batteurs = get_stats_offensives_match(ligne['box_url'], bool(ligne['Est_Domicile']))

        # On conserve les données BRUTES (liste de dicts {name, runs, hr}) dans une
        # colonne cachée, en plus de la version texte formatée pour l'affichage. Toute
        # agrégation ultérieure (ex: résumé des 10 derniers matchs) doit additionner
        # ces valeurs brutes directement, et NE PAS reparser le texte formaté
        # ci-dessous : reparser une chaîne comme "Sanchez (2), Okamoto (1)" est fragile
        # (virgules dans certains noms, suffixe de désambiguïsation qui peut varier
        # d'un match à l'autre pour un même joueur, etc.).
        colonne_stats_brutes.append(stats_batteurs)

        entrees_runs = [f"{s['name']} ({s['runs']})" for s in stats_batteurs if s['runs'] > 0]
        colonne_joueurs_runs.append(", ".join(entrees_runs) if entrees_runs else "—")

        entrees_hr = [f"{s['name']} ({s['hr']})" for s in stats_batteurs if s['hr'] > 0]
        colonne_joueurs_hr.append(", ".join(entrees_hr) if entrees_hr else "—")

        for s in stats_batteurs:
            if s['runs'] > 0:
                cumul_runs[s['name']] = cumul_runs.get(s['name'], 0) + s['runs']
            if s['hr'] > 0:
                cumul_hr[s['name']] = cumul_hr.get(s['name'], 0) + s['hr']

    df['Joueurs (Runs)'] = colonne_joueurs_runs
    df['Joueurs (HR)'] = colonne_joueurs_hr
    df['_offensive_stats'] = colonne_stats_brutes  # colonne interne (non affichée) : liste de dicts {name, runs, hr}

    df_meilleurs_runs = pd.DataFrame(
        [{'Joueur': nom, 'Runs Marqués': total} for nom, total in cumul_runs.items()]
    )
    if not df_meilleurs_runs.empty:
        df_meilleurs_runs = df_meilleurs_runs.sort_values('Runs Marqués', ascending=False).reset_index(drop=True)

    df_meilleurs_hr = pd.DataFrame(
        [{'Joueur': nom, 'Home Runs': total} for nom, total in cumul_hr.items()]
    )
    if not df_meilleurs_hr.empty:
        df_meilleurs_hr = df_meilleurs_hr.sort_values('Home Runs', ascending=False).reset_index(drop=True)

    return df, df_meilleurs_runs, df_meilleurs_hr


def parser_cellule_joueurs(cellule: str) -> dict:
    """
    Parse une cellule du type "Nom (N), Nom2 (N2), ..." et retourne un dict {nom: total}.

    Une cellule peut contenir plusieurs joueurs séparés par des virgules. Certains noms
    (retranscriptions de noms étrangers, suffixes) peuvent eux-mêmes contenir une
    virgule, donc on ne peut pas simplement découper sur toutes les virgules : on
    découpe plutôt sur chaque entrée complète "... (N)" (recherche non-gourmande
    jusqu'à la prochaine parenthèse de valeur).
    """
    cumul = {}
    if not cellule or cellule == "—":
        return cumul

    entrees = re.findall(r'(.+?\(\d+\))(?:,\s*|$)', cellule)
    for entree in entrees:
        entree = entree.strip()
        if not entree:
            continue
        correspondance = re.match(r'^(.*)\((\d+)\)$', entree)
        if correspondance:
            nom = correspondance.group(1).strip()
            valeur = int(correspondance.group(2))
        else:
            nom = entree
            valeur = 1
        cumul[nom] = cumul.get(nom, 0) + valeur

    return cumul


def calculer_resume_10_derniers_matchs(df_derniers: pd.DataFrame):
    """
    À partir des données des 10 derniers matchs, calcule, pour les runs ET pour les
    home runs : la moyenne marquée sur ces matchs, le cumul EXACT par joueur, et le
    top 3 des joueurs les plus récurrents.

    --- CORRECTIF (totaux par joueur incorrects) ---
    Auparavant, cette fonction reparsait les colonnes texte déjà formatées
    ('Joueurs (Runs)' / 'Joueurs (HR)', ex: "Pederson (2), Burger (1)") avec une
    regex pour reconstituer les totaux. Cette approche est fragile - un même nom
    peut apparaître avec un suffixe de désambiguïsation différent d'un match à
    l'autre ("Duran" vs "Duran, E"), ce qui pouvait faire diverger silencieusement
    la somme calculée du contenu réel du tableau affiché.

    La fonction additionne maintenant DIRECTEMENT les statistiques brutes par match
    (colonne interne '_offensive_stats', une liste de dicts {name, runs, hr} par
    match - la même source que celle utilisée pour construire les colonnes
    affichées), sans repasser par aucun texte formaté. Le total obtenu correspond
    donc toujours exactement à la somme des valeurs visibles dans le tableau des
    10 derniers matchs. Le parsing par regex (`parser_cellule_joueurs`) n'est
    conservé qu'en repli, si jamais la colonne brute n'est pas disponible.

    Retourne (moyenne_runs, top3_runs, moyenne_hr, top3_hr, cumul_runs, cumul_hr) :
      - top3_* est une liste de tuples (nom, total) limitée aux 3 plus hauts totaux.
      - cumul_runs / cumul_hr sont les dictionnaires COMPLETS {nom: total} (non
        tronqués), à utiliser dès qu'on a besoin du total exact d'un joueur qui
        n'est pas forcément dans le top 3 de l'AUTRE catégorie.
    """
    if df_derniers.empty or 'R' not in df_derniers.columns:
        return None, [], None, [], {}, {}

    moyenne_runs = pd.to_numeric(df_derniers['R'], errors='coerce').mean()

    a_stats_brutes = '_offensive_stats' in df_derniers.columns
    a_colonne_hr = 'Joueurs (HR)' in df_derniers.columns

    cumul_runs = {}
    cumul_hr = {}

    if a_stats_brutes:
        for stats_match in df_derniers['_offensive_stats']:
            for s in (stats_match or []):
                if s.get('runs', 0) > 0:
                    cumul_runs[s['name']] = cumul_runs.get(s['name'], 0) + s['runs']
                if s.get('hr', 0) > 0:
                    cumul_hr[s['name']] = cumul_hr.get(s['name'], 0) + s['hr']
    else:
        # Repli (rétro-compatibilité) : si la colonne brute n'existe pas, on
        # retombe sur le parsing texte, moins fiable mais fonctionnel.
        for cellule in df_derniers.get('Joueurs (Runs)', []):
            for nom, valeur in parser_cellule_joueurs(cellule).items():
                cumul_runs[nom] = cumul_runs.get(nom, 0) + valeur
        if a_colonne_hr:
            for cellule in df_derniers['Joueurs (HR)']:
                for nom, valeur in parser_cellule_joueurs(cellule).items():
                    cumul_hr[nom] = cumul_hr.get(nom, 0) + valeur

    top3_runs = sorted(cumul_runs.items(), key=lambda x: x[1], reverse=True)[:3]

    moyenne_hr = None
    top3_hr = []
    if a_stats_brutes or a_colonne_hr:
        nb_matchs = len(df_derniers)
        moyenne_hr = (sum(cumul_hr.values()) / nb_matchs) if nb_matchs else 0.0
        top3_hr = sorted(cumul_hr.items(), key=lambda x: x[1], reverse=True)[:3]

    return moyenne_runs, top3_runs, moyenne_hr, top3_hr, cumul_runs, cumul_hr


@st.cache_data(show_spinner=False, ttl=300)
def obtenir_calendrier_du_jour_jst():
    """
    Récupère le calendrier NPB de la date du jour AU JAPON (fuseau JST), pas la date
    française. C'est le cœur de l'adaptation du fuseau horaire : au moment où un
    utilisateur français ouvre l'application le matin, il est déjà "demain après-midi/
    soir" au Japon la plupart du temps, donc interroger le calendrier NPB avec la date
    française donnerait très souvent le mauvais jour de match (voire aucun match).
    """
    maintenant_jst = datetime.now(TZ_JST)
    df_mois = charger_calendrier_mensuel(maintenant_jst.year, maintenant_jst.month)
    if df_mois.empty:
        return pd.DataFrame(), maintenant_jst
    date_str = maintenant_jst.strftime('%Y-%m-%d')
    return df_mois[df_mois['Date'] == date_str].copy(), maintenant_jst


@st.cache_data(show_spinner=False, ttl=1800)
def _charger_lanceurs_annonces():
    """
    Scrape la page officielle des lanceurs partants annoncés ("予告先発投手") :
    https://npb.jp/announcement/starter/

    Au Japon, les lanceurs partants sont annoncés la VEILLE pour le lendemain : cette
    page contient donc, en pratique, les partants du prochain jour de matchs - ce qui
    correspond exactement au "match du jour" (JST) recherché par cette application.

    Retourne un dict {code_equipe_minuscule: (nom_lanceur, id_lanceur_npb)}.
    """
    url = "https://npb.jp/announcement/starter/"
    resultat = {}
    try:
        soup = appeler_avec_retry(_get_soup, url)
    except Exception:
        return resultat

    for unit in soup.select('div.unit'):
        for cote in ('team_left', 'team_right'):
            bloc = unit.find('div', class_=cote)
            if bloc is None:
                continue
            img = bloc.find('img')
            if img is None or not img.get('src'):
                continue
            m_code = re.search(r'logo_([a-z]+)_m\.gif', img['src'])
            if not m_code:
                continue
            code = m_code.group(1)

            lien_joueur = bloc.find('a', href=re.compile(r'/bis/players/\d+\.html'))
            if lien_joueur is None:
                continue
            m_id = re.search(r'/bis/players/(\d+)\.html', lien_joueur['href'])
            if not m_id:
                continue

            nom = lien_joueur.get_text(strip=True)
            if nom:
                resultat[code] = (nom, m_id.group(1))

    return resultat


def obtenir_lanceur_annonce(code_equipe: str):
    """Retourne (nom_lanceur, id_lanceur_npb) pour le code équipe donné, ou (None, None)."""
    if not code_equipe:
        return None, None
    return _charger_lanceurs_annonces().get(code_equipe.lower(), (None, None))


@st.cache_data(show_spinner=False, ttl=300)
def obtenir_match_du_jour(code_equipe: str):
    """
    Cherche, dans le calendrier NPB du jour (date système AU JAPON, cf.
    `obtenir_calendrier_du_jour_jst`), un match impliquant l'équipe donnée. Retourne un
    dict avec l'adversaire, le statut domicile/extérieur, les lanceurs partants
    annoncés (des deux côtés), le stade, l'heure JST ET l'heure française équivalente,
    ou None si aucun match n'est prévu aujourd'hui (JST) pour cette équipe.
    """
    if not code_equipe:
        return None

    df_jour, maintenant_jst = obtenir_calendrier_du_jour_jst()
    if df_jour.empty:
        return None

    code_equipe = code_equipe.lower()
    ligne = None
    for _, g in df_jour.iterrows():
        if g['code_home'] == code_equipe or g['code_away'] == code_equipe:
            ligne = g
            break
    if ligne is None:
        return None

    est_domicile = (ligne['code_home'] == code_equipe)
    code_adverse = ligne['code_away'] if est_domicile else ligne['code_home']
    nom_adverse = TEAMS_NPB.get(
        (code_adverse or "").upper(),
        (ligne['nom_away'] if est_domicile else ligne['nom_home'])
    )

    # --- Conversion JST -> heure française (Europe/Paris) ---
    # C'est le point clé demandé : on affiche l'heure de coup d'envoi à la fois en
    # heure locale japonaise (celle utilisée par npb.jp) et en heure française, pour
    # que l'utilisateur sache immédiatement à quelle heure (française) suivre le match.
    heure_jst_str = (ligne.get('heure_jst') or "").strip()
    heure_paris_str = None
    if re.match(r'^\d{1,2}:\d{2}$', heure_jst_str):
        try:
            h, m = map(int, heure_jst_str.split(':'))
            dt_jst = datetime(maintenant_jst.year, maintenant_jst.month, maintenant_jst.day, h, m, tzinfo=TZ_JST)
            dt_paris = dt_jst.astimezone(TZ_PARIS)
            # On affiche systématiquement la date française complète (et pas
            # seulement l'heure) : comme il peut déjà être "demain" au Japon par
            # rapport au jour civil français au moment où l'utilisateur consulte
            # l'app, indiquer uniquement "11:00" serait ambigu. "31/07 à 11:00"
            # lève toute ambiguïté sur le jour civil français correspondant.
            heure_paris_str = dt_paris.strftime('%d/%m à %H:%M')
        except Exception:
            heure_paris_str = None

    # Le lanceur annoncé est cherché en priorité sur la page dédiée
    # "/announcement/starter/" (elle seule fournit l'identifiant npb.jp du joueur,
    # nécessaire pour aller chercher ses statistiques ensuite). Si cette page ne le
    # liste pas encore (ex: décalage de publication) mais que la page de calendrier
    # du jour l'a déjà (colonne "先発" - starter annoncé), on utilise ce nom en
    # repli pour l'affichage (sans stats détaillées, faute d'identifiant joueur).
    lanceur_notre_equipe, _ = obtenir_lanceur_annonce(code_equipe)
    lanceur_adverse, id_lanceur_adverse = obtenir_lanceur_annonce(code_adverse)

    lanceur_annonce_home = (ligne.get('lanceur_annonce_home') or "").strip()
    lanceur_annonce_away = (ligne.get('lanceur_annonce_away') or "").strip()
    lanceur_annonce_notre_equipe = lanceur_annonce_home if est_domicile else lanceur_annonce_away
    lanceur_annonce_adverse = lanceur_annonce_away if est_domicile else lanceur_annonce_home

    if not lanceur_notre_equipe and lanceur_annonce_notre_equipe:
        lanceur_notre_equipe = lanceur_annonce_notre_equipe
    if not lanceur_adverse and lanceur_annonce_adverse:
        lanceur_adverse = lanceur_annonce_adverse

    score_home, score_away = ligne.get('score_home'), ligne.get('score_away')
    if pd.notna(score_home) and pd.notna(score_away):
        statut = "Terminé" if (ligne.get('lanceur_gagnant') or ligne.get('lanceur_perdant')) else "En cours"
    else:
        statut = "Programmé"

    return {
        'adversaire': nom_adverse,
        'est_domicile': est_domicile,
        'lanceur_notre_equipe': lanceur_notre_equipe,
        'lanceur_adverse': lanceur_adverse,
        'id_lanceur_adverse': id_lanceur_adverse,
        'heure_jst': heure_jst_str or "—",
        'heure_paris': heure_paris_str or "—",
        'statut': statut,
        'venue': ligne.get('lieu') or "—",
    }


@st.cache_data(show_spinner=False, ttl=3600)
def obtenir_stats_lanceur(nom_lanceur: str, id_lanceur: str, annee: int):
    """
    Récupère, via la fiche joueur officielle npb.jp (https://npb.jp/bis/players/{id}.html),
    les statistiques de la saison en cours du lanceur (ERA, WHIP calculé, runs alloués,
    HR alloués, HR/9, nombre d'apparitions comme approximation du nombre de départs).

    NPB.jp n'affiche pas de WHIP ni de HR/9 tout faits sur cette page (contrairement à
    MLB StatsAPI) : ils sont donc calculés ici à partir des statistiques brutes
    publiées (安打=hits alloués, 四球=BB alloués, 投球回=manches lancées, 本塁打=HR alloués).

    Retourne None si l'id est vide, si le lanceur n'a pas de ligne de stats pour
    `annee`, ou si les données sont insuffisantes (ex: lanceur de relève sans
    historique exploitable).
    """
    if not id_lanceur:
        return None
    url = f"https://npb.jp/bis/players/{id_lanceur}.html"
    try:
        soup = appeler_avec_retry(_get_soup, url)
    except Exception:
        return None

    table = soup.find('table', id='tablefix_p')
    if table is None:
        return None
    tbody = table.find('tbody')
    if tbody is None:
        return None

    ligne_annee = None
    for tr in tbody.find_all('tr'):
        td_annee = tr.find('td', class_='year')
        if td_annee and td_annee.get_text(strip=True) == str(annee):
            ligne_annee = tr
    if ligne_annee is None:
        return None

    tds = ligne_annee.find_all('td', recursive=False)
    # Colonnes (ordre fixe affiché par npb.jp) :
    # 0:année 1:équipe 2:apparitions 3:V 4:D 5:Sv 6:Hold 7:HoldPt 8:CG 9:ShO
    # 10:sansBB 11:%V 12:BF 13:IP(tableau imbriqué) 14:H 15:HR 16:BB 17:HBP
    # 18:SO 19:WP 20:Balk 21:R 22:ER 23:ERA
    if len(tds) < 24:
        return None

    def _txt(i):
        return tds[i].get_text(strip=True)

    try:
        apparitions = int(_txt(2) or 0)
        hits_alloues = int(_txt(14) or 0)
        hr_alloues = int(_txt(15) or 0)
        bb_alloues = int(_txt(16) or 0)
        runs_alloues = int(_txt(21) or 0)
        era = float(_txt(23) or 0)
    except ValueError:
        return None

    if not era:
        return None

    # Manches lancées : encodées dans un mini-tableau imbriqué à l'intérieur de la
    # cellule "投球回" (entier de manches en <th>, fraction de manche ".1"/".2" en <td>).
    manches_entieres, tiers = 0, 0
    cellule_ip = tds[13]
    th_manches = cellule_ip.find('th')
    td_fraction = cellule_ip.find('td')
    if th_manches and th_manches.get_text(strip=True).isdigit():
        manches_entieres = int(th_manches.get_text(strip=True))
    if td_fraction:
        fraction_txt = td_fraction.get_text(strip=True)
        if fraction_txt == '.1':
            tiers = 1
        elif fraction_txt == '.2':
            tiers = 2
    innings_lancees = manches_entieres + tiers / 3.0
    if innings_lancees <= 0:
        return None

    whip = (hits_alloues + bb_alloues) / innings_lancees
    hr_par_9 = (hr_alloues / innings_lancees) * 9

    return {
        'nom': nom_lanceur or "Lanceur adverse",
        'era': era,
        'whip': whip,
        'runs_alloues': runs_alloues,
        'hr_alloues': hr_alloues,
        'hr_par_9': hr_par_9,
        'matchs_titulaire': apparitions,
    }


def predire_runs_match(moyenne_runs_equipe, moyenne_ra_equipe, stats_lanceur_adverse):
    """
    Estimation heuristique (PAS un modèle statistique validé) du nombre de runs que
    l'équipe sélectionnée pourrait marquer aujourd'hui, ainsi que du total de runs
    du match, en croisant :
      - la moyenne de runs marqués par l'équipe sur ses 10 derniers matchs,
      - les stats du lanceur partant adverse (ERA, WHIP) - un ERA/WHIP élevé
        indique un lanceur plus "battable", donc on augmente l'estimation,
      - la moyenne de runs concédés par l'équipe sur ses 10 derniers matchs,
        utilisée comme proxy raisonnable de l'attaque adverse (faute de connaître
        le lanceur partant de notre propre équipe, hors périmètre de la demande).
    Retourne un dict {'runs_equipe', 'total_match', 'confiance'} ou None si aucune
    donnée de forme récente n'est disponible pour l'équipe.
    """
    if moyenne_runs_equipe is None:
        return None

    if stats_lanceur_adverse is not None and stats_lanceur_adverse.get('era', 0) > 0:
        era = stats_lanceur_adverse['era']
        whip = stats_lanceur_adverse['whip']
        # Moyenne pondérée entre la forme offensive de l'équipe et la vulnérabilité du lanceur adverse
        runs_estimes_equipe = (moyenne_runs_equipe * 0.55) + (era * 0.45)
        # Un WHIP élevé (plus de coureurs sur les buts) augmente l'estimation, un WHIP très bas la réduit
        if whip >= 1.35:
            runs_estimes_equipe *= 1.12
        elif whip <= 1.05:
            runs_estimes_equipe *= 0.90
        confiance = "Élevée" if stats_lanceur_adverse.get('matchs_titulaire', 0) >= 8 else "Moyenne"
    else:
        # Pas de stats fiables sur le lanceur adverse -> on se base uniquement sur la forme offensive de l'équipe
        runs_estimes_equipe = moyenne_runs_equipe
        confiance = "Faible"

    runs_estimes_adverse = moyenne_ra_equipe if moyenne_ra_equipe is not None and pd.notna(moyenne_ra_equipe) else moyenne_runs_equipe
    total_runs_estime = runs_estimes_equipe + runs_estimes_adverse

    return {
        'runs_equipe': round(runs_estimes_equipe, 1),
        'total_match': round(total_runs_estime, 1),
        'confiance': confiance,
    }


def predire_joueurs_du_jour(cumul_runs_10, cumul_hr_10, stats_lanceur_adverse, top_n: int = 3):
    """
    Construit une liste de joueurs "en forme" et calcule pour chacun un indice de
    confiance (0-100) croisant leur activité récente avec les faiblesses du
    lanceur adverse du jour (ERA, WHIP, HR/9 encaissés).

    --- CORRECTIF (un joueur pouvait afficher "0 run" alors qu'il en avait marqué) ---
    Cette fonction recevait auparavant uniquement les listes TOP 3 (top3_runs_10 /
    top3_hr_10, déjà tronquées à 3 éléments chacune). Un joueur présent dans le
    top 3 des HR mais pas dans le top 3 des runs (car d'autres joueurs avaient
    plus de runs) se voyait donc afficher "0 run" même s'il avait réellement
    marqué plusieurs runs sur les 10 derniers matchs.

    La fonction prend maintenant directement `cumul_runs_10` / `cumul_hr_10`, les
    dictionnaires COMPLETS (non tronqués) de tous les joueurs. On sélectionne les
    candidats "en forme" via le top 3 de chaque catégorie (comme avant), mais on
    va chercher leur total EXACT (runs ET HR) dans ces dictionnaires complets, ce
    qui garantit un affichage fidèle au tableau des 10 derniers matchs.

    Retourne une liste de dicts triée par indice décroissant, limitée à `top_n`.
    """
    cumul_runs_10 = cumul_runs_10 or {}
    cumul_hr_10 = cumul_hr_10 or {}

    if not cumul_runs_10 and not cumul_hr_10:
        return []

    # Candidats "en forme" = présents dans le top 3 d'AU MOINS une des deux
    # catégories (runs ou HR) - mais leur total affiché sera toujours le total
    # RÉEL (les deux dictionnaires complets), jamais une valeur tronquée à 0.
    top3_noms_runs = {nom for nom, _ in sorted(cumul_runs_10.items(), key=lambda x: x[1], reverse=True)[:3]}
    top3_noms_hr = {nom for nom, _ in sorted(cumul_hr_10.items(), key=lambda x: x[1], reverse=True)[:3]}
    candidats = top3_noms_runs | top3_noms_hr

    if not candidats:
        return []

    # Facteur de vulnérabilité du lanceur adverse : plus son ERA/WHIP/HR-par-9 sont
    # élevés, plus il est jugé "battable" (facteur > 1) ; un lanceur dominant réduit
    # le facteur (< 1). Le facteur est borné pour rester réaliste (pas d'emballement).
    facteur_adverse = 1.0
    if stats_lanceur_adverse is not None and stats_lanceur_adverse.get('era', 0) > 0:
        era = stats_lanceur_adverse['era']
        whip = stats_lanceur_adverse['whip']
        hr9 = stats_lanceur_adverse['hr_par_9']
        facteur_adverse += max(0, (era - 4.0)) * 0.08
        facteur_adverse += max(0, (whip - 1.20)) * 0.5
        facteur_adverse += max(0, (hr9 - 1.0)) * 0.15
        facteur_adverse = max(0.7, min(facteur_adverse, 1.6))

    resultats = []
    for nom in candidats:
        runs_10 = cumul_runs_10.get(nom, 0)
        hr_10 = cumul_hr_10.get(nom, 0)
        indice_brut = (runs_10 * 8) + (hr_10 * 20)  # le HR pèse plus car plus rare qu'un run
        indice = min(95, round(indice_brut * facteur_adverse))
        if indice <= 0:
            continue
        if indice >= 65:
            confiance = "Élevée"
        elif indice >= 35:
            confiance = "Moyenne"
        else:
            confiance = "Faible"
        resultats.append({
            'nom': nom,
            'runs_10': runs_10,
            'hr_10': hr_10,
            'indice': indice,
            'confiance': confiance,
        })

    resultats = sorted(resultats, key=lambda x: x['indice'], reverse=True)
    return resultats[:top_n]


# ============================================================
# 5. INTERFACE PRINCIPALE
# ============================================================

st.title("⚾ Analyse Statistiques NPB (Nippon Professional Baseball)")
st.markdown("### Explorez les runs, les prédictions du jour et les tendances W/L")

# Sidebar pour les paramètres globaux
with st.sidebar:
    st.header("⚙️ Paramètres")
    saison_options = list(range(ANNEE_COURANTE, ANNEE_COURANTE - 5, -1))
    annee = int(st.selectbox(
        "Sélectionnez la saison:",
        options=saison_options,
        index=0
    ))
    st.markdown("---")
    st.markdown("**Légende des abréviations:**")
    st.markdown("""
    - **R** : Runs (Points marqués)
    - **RA** : Runs Against (Points concédés)
    - **HR** : Home Runs (Coup de circuit)
    - **W** : Wins (Victoires)
    - **L** : Losses (Défaites)
    """)
    st.markdown("---")
    st.caption(
        "🕒 Les dates/heures de match sont gérées en heure du Japon (JST, UTC+9, sans "
        "heure d'été) puis converties en heure française dans l'onglet Prédictions du "
        "jour, car les matchs NPB se jouent souvent tôt le matin en heure française."
    )
    st.caption("📡 Données récupérées directement depuis le site officiel npb.jp.")

# Récupération de la liste des équipes NPB
EQUIPES_NPB = get_teams_npb(annee)

# ============================================================
# 6. ONGLETS PRINCIPAUX
# ============================================================
onglets = st.tabs([
    "📊 Analyse par Équipe",
    "🔮 Prédictions du jour"
])

# --------------------------------------------------------------
# ONGLET 1: ANALYSE PAR ÉQUIPE
# --------------------------------------------------------------
with onglets[0]:
    st.header("📊 Analyse des Runs par Équipe")

    col1, col2 = st.columns([1, 3])

    with col1:
        options_equipes = [f"{abbr} - {nom}" for abbr, nom in EQUIPES_NPB.items()]
        equipe_selectionnee = st.selectbox(
            "Choisissez une équipe:",
            options=options_equipes
        )

    equipe_abbr = extraire_abreviation_equipe(equipe_selectionnee)

    # Chargement des données de matchs, enrichies avec les scoreurs de runs et de HR
    # (boxscores npb.jp) - premier chargement potentiellement long (un appel réseau
    # par match de la saison), les chargements suivants sont quasi instantanés grâce
    # au cache Streamlit.
    with st.spinner(f"Chargement des données et des boxscores pour les {EQUIPES_NPB[equipe_abbr]} ({annee})... (peut prendre un moment)"):
        df_matchs, df_meilleurs_scoreurs, df_meilleurs_hr = get_matchs_avec_scoreurs(annee, equipe_abbr)

    # Valeurs par défaut du résumé des 10 derniers matchs : elles sont réaffectées plus bas
    # si les données sont disponibles, mais doivent exister dès maintenant car l'onglet
    # "Prédictions du jour" (exécuté après celui-ci) les réutilise.
    moyenne_runs_10, top3_runs_10, moyenne_hr_10, top3_hr_10 = None, [], None, []
    cumul_runs_10, cumul_hr_10 = {}, {}

    st.markdown("---")
    st.subheader("🔝 Classement Home Runs dans l'équipe")

    # -------- Top 3 frappeurs de Home Runs, calculé à partir des boxscores npb.jp --------
    # NPB.jp n'expose pas de "stats de saison par joueur du roster" en un seul appel
    # (contrairement à MLB StatsAPI) : plutôt que de faire un appel réseau par joueur
    # du roster (~40 requêtes), on réutilise directement `df_meilleurs_hr`, déjà
    # calculé ci-dessus à partir de TOUS les boxscores de la saison chargée -
    # c'est la même source de vérité que la colonne "Joueurs (HR)" plus bas.
    top_batteurs_hr = []
    if not df_meilleurs_hr.empty:
        top_batteurs_hr = df_meilleurs_hr.head(3).to_dict('records')

    if not top_batteurs_hr:
        st.info("Aucun joueur avec Home Runs enregistré pour cette équipe/saison.")
    else:
        slugger_cols = st.columns(len(top_batteurs_hr))
        for idx, row in enumerate(top_batteurs_hr):
            with slugger_cols[idx]:
                st.metric(label=row['Joueur'], value=f"{int(row['Home Runs'])} HR")
    # ----- Fin du classement Home Runs équipe ------

    st.markdown("---")
    st.subheader("📈 Tendance des Runs par match (score équipe)")
    # 2. Graphique tendance Runs, avec ligne de moyenne annotée
    try:
        if not df_matchs.empty and "R" in df_matchs.columns:
            df_matchs = df_matchs.copy()
            df_matchs['Runs'] = pd.to_numeric(df_matchs['R'], errors='coerce')
            df_matchs = df_matchs.dropna(subset=['Runs'])
            # Ajouter un numéro de match croissant
            df_matchs = df_matchs.reset_index(drop=True)
            df_matchs['Match_Num'] = df_matchs.index + 1

            if not df_matchs.empty:
                moyenne_runs = df_matchs['Runs'].mean()

                ligne_runs = alt.Chart(df_matchs).mark_line(
                    point=True, color='#1f77b4'
                ).encode(
                    x=alt.X('Match_Num:Q', title='Numéro du match'),
                    y=alt.Y('Runs:Q', title='Runs marqués'),
                    tooltip=[
                        alt.Tooltip('Match_Num:Q', title='Match #'),
                        alt.Tooltip('Runs:Q', title='Runs')
                    ]
                )

                ligne_moyenne = alt.Chart(pd.DataFrame({'moyenne': [moyenne_runs]})).mark_rule(
                    color='red', strokeDash=[6, 4], size=2
                ).encode(
                    y=alt.Y('moyenne:Q'),
                    tooltip=[alt.Tooltip('moyenne:Q', title='Moyenne', format='.2f')]
                )

                annotation_moyenne = alt.Chart(pd.DataFrame({
                    'moyenne': [moyenne_runs],
                    'x': [df_matchs['Match_Num'].max()]
                })).mark_text(
                    text=f"Moyenne : {moyenne_runs:.2f}",
                    align='right',
                    baseline='bottom',
                    dx=-4,
                    dy=-6,
                    color='red',
                    fontWeight='bold'
                ).encode(
                    x=alt.X('x:Q'),
                    y=alt.Y('moyenne:Q')
                )

                st.altair_chart(ligne_runs + ligne_moyenne + annotation_moyenne)
            else:
                st.info("Pas de données de runs disponibles pour cette équipe/saison.")
        else:
            st.info("Pas de données de runs disponibles pour cette équipe/saison.")
    except Exception as e:
        st.info(f"Erreur lors de l'affichage des tendances de runs : {e}")

    # Statistiques synthétiques en haut
    if not df_matchs.empty and 'R' in df_matchs.columns:
        runs_total = df_matchs['R'].sum()
        runs_moyen = df_matchs['R'].mean()
        matchs_joues = len(df_matchs[df_matchs['R'].notna()])

        st.markdown(f"### Statistiques des Runs - {EQUIPES_NPB[equipe_abbr]} ({annee})")
        col_stat1, col_stat2, col_stat3 = st.columns(3)
        with col_stat1:
            st.metric(
                label="Runs Totaux",
                value=f"{int(runs_total)}",
                help="Nombre total de runs marqués cette saison")
        with col_stat2:
            st.metric(
                label="Moyenne par Match",
                value=f"{runs_moyen:.2f}",
                help="Moyenne de runs marqués par match"
            )
        with col_stat3:
            st.metric(
                label="Matchs Analysés",
                value=f"{matchs_joues}",
                help="Nombre de matchs avec données disponibles"
            )

        st.markdown("---")
        st.subheader("📋 Derniers Matchs")
        st.caption("Dates affichées selon le calendrier japonais (JST), identique à celui de npb.jp.")
        display_columns = ['Date', 'Équipe Domicile', 'Équipe Extérieur', 'R', 'RA', 'W/L', 'Joueurs (Runs)', 'Joueurs (HR)']
        df_recents = df_matchs.tail(10)
        df_recents = df_recents[display_columns] if all(c in df_recents.columns for c in display_columns) else df_recents

        # Renommer les colonnes pour la présentation
        df_recents = df_recents.rename(columns={
            'R': 'Runs',
            'RA': 'Runs_Adverses',
            'W/L': 'Résultat'
        })

        # --- Ajout du surlignage sur l'équipe sélectionnée dans le tableau des matchs ---

        # Nom de l'équipe sélectionnée (utilisé pour la surbrillance)
        nom_equipe_sel = EQUIPES_NPB.get(equipe_abbr, "")

        def highlight_team(cell):
            if cell == nom_equipe_sel:
                # On utilise un bleu claire qui convient sur clair comme foncé
                return 'background-color: #bdd7ee; font-weight: bold;'
            return ''

        # Affichage du DataFrame stylé
        try:
            st.dataframe(
                df_recents.style.applymap(
                    highlight_team,
                    subset=['Équipe Domicile', 'Équipe Extérieur']
                ),
                use_container_width=True,
                hide_index=True
            )
        except Exception:
            st.dataframe(df_recents, use_container_width=True, hide_index=True)

        # --- Résumé permanent des 10 derniers matchs (se met à jour automatiquement) ---
        moyenne_runs_10, top3_runs_10, moyenne_hr_10, top3_hr_10, cumul_runs_10, cumul_hr_10 = calculer_resume_10_derniers_matchs(
            df_matchs.tail(10)
        )
        if moyenne_runs_10 is not None:
            texte_top3_runs = (
                ", ".join(f"{nom} ({runs} runs)" for nom, runs in top3_runs_10)
                if top3_runs_10 else "Aucun joueur enregistré"
            )
            texte_top3_hr = (
                ", ".join(f"{nom} ({hr} HR)" for nom, hr in top3_hr_10)
                if top3_hr_10 else "Aucun joueur enregistré"
            )

            st.markdown(f"**Moyenne de runs sur les 10 derniers matchs : {moyenne_runs_10:.2f}**")
            st.markdown(f"**Top 3 des joueurs les plus récurrents (runs marqués) : {texte_top3_runs}**")
            st.markdown(f"**Moyenne de home runs sur les 10 derniers matchs : {moyenne_hr_10:.2f}**")
            st.markdown(f"**Top 3 des joueurs les plus récurrents (home runs) : {texte_top3_hr}**")
        else:
            st.markdown("**Résumé indisponible : pas assez de données sur les 10 derniers matchs.**")

        st.markdown("---")
        col_runs, col_hr = st.columns(2)
        with col_runs:
            st.subheader("🏅 Meilleurs scoreurs de Runs")
            st.markdown(f"Cumul des runs marqués par joueur sur la saison {annee}")
            if not df_meilleurs_scoreurs.empty:
                st.dataframe(
                    df_meilleurs_scoreurs,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info("Aucune donnée de scoreurs disponible pour cette équipe/saison.")
        with col_hr:
            st.subheader("🏆 Meilleurs frappeurs de Home Runs")
            st.markdown(f"Cumul des home runs marqués par joueur sur la saison {annee}")
            if not df_meilleurs_hr.empty:
                st.dataframe(
                    df_meilleurs_hr,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info("Aucune donnée de home runs disponible pour cette équipe/saison.")
    elif not df_matchs.empty:
        st.warning("Données de runs non disponibles pour cette équipe.")
    else:
        st.error("Impossible de charger les données. Vérifiez le code de l'équipe, ou réessayez : npb.jp peut être temporairement indisponible.")

# --------------------------------------------------------------
# ONGLET 2: PRÉDICTIONS DU JOUR
# --------------------------------------------------------------
with onglets[1]:
    st.header("🔮 Prédictions du jour")
    st.markdown(f"Prédiction du match du jour pour les **{EQUIPES_NPB.get(equipe_abbr, equipe_abbr)}**")
    st.caption(
        "⚠️ Estimations statistiques basées sur les tendances récentes de l'équipe et les stats du "
        "lanceur adverse. Ce ne sont pas des garanties de résultat : à utiliser uniquement à titre "
        "informatif, avec discernement si vous vous en servez pour parier."
    )

    if annee != ANNEE_COURANTE:
        st.info(
            f"Les prédictions du jour ne sont disponibles que pour la saison en cours "
            f"({ANNEE_COURANTE}). Sélectionnez {ANNEE_COURANTE} dans le menu de gauche pour "
            f"voir la prédiction du match d'aujourd'hui (heure du Japon)."
        )
    else:
        maintenant_jst_aff = datetime.now(TZ_JST)
        maintenant_paris_aff = maintenant_jst_aff.astimezone(TZ_PARIS)
        st.caption(
            f"📅 Aujourd'hui au Japon : {maintenant_jst_aff.strftime('%A %d %B %Y, %H:%M')} (JST) "
            f"— soit {maintenant_paris_aff.strftime('%A %d %B %Y, %H:%M')} en France."
        )

        with st.spinner("Recherche du match du jour (calendrier NPB, heure du Japon)..."):
            match_du_jour = obtenir_match_du_jour(equipe_abbr)

        if not match_du_jour:
            st.info(f"Aucun match n'est prévu aujourd'hui (heure du Japon) pour les {EQUIPES_NPB.get(equipe_abbr, equipe_abbr)}.")
        else:
            lieu = "à domicile" if match_du_jour['est_domicile'] else "à l'extérieur"
            st.subheader(
                f"🆚 {EQUIPES_NPB.get(equipe_abbr, equipe_abbr)} {lieu} contre {match_du_jour['adversaire']}"
            )

            col_venue, col_heure_jst, col_heure_paris, col_statut = st.columns(4)
            with col_venue:
                st.metric("Stade", match_du_jour['venue'] or "—")
            with col_heure_jst:
                st.metric("Heure (Japon, JST)", match_du_jour['heure_jst'])
            with col_heure_paris:
                st.metric("Heure (France)", match_du_jour['heure_paris'])
            with col_statut:
                st.metric("Statut", match_du_jour['statut'] or "—")

            st.markdown("#### ⚾ Lanceurs partants annoncés")
            col_p1, col_p2 = st.columns(2)
            with col_p1:
                st.markdown(f"**{EQUIPES_NPB.get(equipe_abbr, equipe_abbr)}**")
                st.markdown(f"### {match_du_jour['lanceur_notre_equipe'] or 'Non annoncé'}")
            with col_p2:
                st.markdown(f"**{match_du_jour['adversaire']}**")
                st.markdown(f"### {match_du_jour['lanceur_adverse'] or 'Non annoncé'}")

            with st.spinner("Analyse des statistiques du lanceur adverse..."):
                stats_lanceur_adverse = obtenir_stats_lanceur(
                    match_du_jour['lanceur_adverse'],
                    match_du_jour['id_lanceur_adverse'],
                    annee,
                )

            if stats_lanceur_adverse:
                st.caption(
                    f"Stats saison {annee} de {stats_lanceur_adverse['nom']} : "
                    f"ERA {stats_lanceur_adverse['era']:.2f} · WHIP {stats_lanceur_adverse['whip']:.2f} · "
                    f"{stats_lanceur_adverse['hr_alloues']} HR alloués · "
                    f"{stats_lanceur_adverse['matchs_titulaire']} apparitions"
                )
            elif match_du_jour['lanceur_adverse']:
                st.caption("Statistiques du lanceur adverse indisponibles pour le moment.")

            st.markdown("---")
            st.subheader("📊 Module de prédiction des Runs")

            if moyenne_runs_10 is None:
                st.info("Pas assez de données récentes pour estimer les runs de cette équipe.")
            else:
                moyenne_ra_10 = pd.to_numeric(
                    df_matchs.tail(10).get('RA', pd.Series(dtype=float)), errors='coerce'
                ).mean()
                prediction_runs = predire_runs_match(moyenne_runs_10, moyenne_ra_10, stats_lanceur_adverse)

                col_pred1, col_pred2, col_pred3 = st.columns(3)
                with col_pred1:
                    st.metric(
                        f"Runs estimés — {equipe_abbr}",
                        f"{prediction_runs['runs_equipe']}"
                    )
                with col_pred2:
                    st.metric("Total de runs estimé (match)", f"{prediction_runs['total_match']}")
                with col_pred3:
                    st.metric("Indice de confiance", prediction_runs['confiance'])

                st.caption(
                    f"Basé sur une moyenne de {moyenne_runs_10:.2f} runs/match et "
                    f"{moyenne_ra_10:.2f} runs concédés/match sur les 10 derniers matchs, "
                    + (
                        f"croisée avec les stats du lanceur adverse ({stats_lanceur_adverse['nom']})."
                        if stats_lanceur_adverse
                        else "en l'absence de stats fiables sur le lanceur adverse."
                    )
                )

            st.markdown("---")
            st.subheader("🎯 Module de prédiction des Joueurs (HR / Runs)")

            joueurs_a_surveiller = predire_joueurs_du_jour(
                cumul_runs_10, cumul_hr_10, stats_lanceur_adverse, top_n=3
            )

            if not joueurs_a_surveiller:
                st.info(
                    "Pas assez de données de forme récente (runs/HR sur les 10 derniers matchs) "
                    "pour identifier des joueurs à surveiller aujourd'hui."
                )
            else:
                cols_joueurs = st.columns(len(joueurs_a_surveiller))
                for idx, joueur in enumerate(joueurs_a_surveiller):
                    with cols_joueurs[idx]:
                        st.markdown(f"**{joueur['nom']}**")
                        st.progress(joueur['indice'] / 100)
                        st.markdown(f"Indice de confiance : **{joueur['confiance']}** ({joueur['indice']}/100)")
                        st.caption(
                            f"{joueur['runs_10']} run(s) et {joueur['hr_10']} HR sur les 10 derniers matchs"
                        )

# ============================================================
# 7. PIED DE PAGE
# ============================================================
st.markdown("---")
st.markdown(
    f"<div style='text-align: center; color: gray;'>"
    f"⚾ Application NPB Analytics | Données : npb.jp | Mise à jour: "
    f"{datetime.now(TZ_JST).strftime('%Y-%m-%d %H:%M')} JST"
    f"</div>",
    unsafe_allow_html=True
)
