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
import json                     # Sérialisation de l'historique des prédictions (bilan de la veille)
import os                       # Chemin du fichier d'historique des prédictions
import requests                 # Appels HTTP vers npb.jp (scraping)
from bs4 import BeautifulSoup   # Parsing HTML des pages npb.jp
from datetime import datetime, timedelta  # Gestion des dates (timedelta : calcul de "hier")
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

# Fichier local dans lequel est archivé, chaque jour, un instantané des prédictions
# ("Hot Pronostics") du jour - nécessaire pour le "Bilan des Prédictions" de la veille
# (onglet Résumé) : sans base de données externe, il n'existe sinon aucune trace de ce
# que l'algorithme avait annoncé la veille une fois le jour suivant arrivé. Placé à côté
# du script (et non dans le répertoire courant) pour fonctionner quel que soit le
# dossier depuis lequel `streamlit run` est lancé. Fichier généré à l'exécution : listé
# dans `.gitignore`, il n'est donc jamais versionné.
CHEMIN_HISTORIQUE_PREDICTIONS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "historique_predictions_npb.json"
)


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


def _charger_historique_predictions() -> dict:
    """
    Lit `CHEMIN_HISTORIQUE_PREDICTIONS` (fichier JSON local, un instantané par date
    au format {'AAAA-MM-JJ': {'sauvegarde_le': ..., 'matches': [...]}}). Retourne un
    dict vide si le fichier n'existe pas encore (premier lancement de l'application)
    ou s'il est corrompu - ne doit jamais faire planter l'application.
    """
    try:
        with open(CHEMIN_HISTORIQUE_PREDICTIONS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _sauvegarder_predictions_du_jour(date_str: str, matches_snapshot: list) -> None:
    """
    Archive l'instantané des prédictions du jour (`matches_snapshot`) dans
    `CHEMIN_HISTORIQUE_PREDICTIONS`, sous la clé `date_str`. Appelée depuis
    `construire_donnees_hot_pronostics` (donc au maximum une fois toutes les 30 min,
    son propre `ttl` de cache) : écrire à chaque appel écrase simplement l'instantané
    du jour par la version la plus à jour (utile si les lanceurs annoncés changent en
    cours de journée), ce qui est le comportement recherché.

    Purge au passage les entrées de plus de 30 jours, pour que ce fichier ne grossisse
    pas indéfiniment au fil des mois. Ne lève jamais d'exception : la sauvegarde de
    l'historique est un "bonus" (bilan de la veille) qui ne doit jamais faire planter
    le calcul des prédictions du jour lui-même en cas de souci d'écriture disque
    (permissions, disque plein, filesystem éphémère en environnement cloud, etc.).
    """
    try:
        historique = _charger_historique_predictions()
        historique[date_str] = {
            'sauvegarde_le': datetime.now(TZ_JST).isoformat(),
            'matches': matches_snapshot,
        }
        date_limite = (datetime.now(TZ_JST) - timedelta(days=30)).strftime('%Y-%m-%d')
        historique = {d: v for d, v in historique.items() if d >= date_limite}
        with open(CHEMIN_HISTORIQUE_PREDICTIONS, "w", encoding="utf-8") as f:
            json.dump(historique, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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

# Traduction des noms de stades (affichés en japonais sur les pages de calendrier
# utilisées) vers leur nom anglais usuel. Couvre les 12 stades "domicile" des
# équipes NPB, utilisés l'immense majorité du temps ; les stades régionaux plus
# rares (matchs "hors les murs" organisés quelques fois par saison) ne sont pas
# tous listés - dans ce cas, le nom japonais d'origine est affiché en repli.
STADES_NPB = {
    "東京ドーム": "Tokyo Dome",
    "甲子園": "Koshien Stadium",
    "横浜スタジアム": "Yokohama Stadium",
    "バンテリンドーム": "Vantelin Dome Nagoya",
    "バンテリンドームナゴヤ": "Vantelin Dome Nagoya",
    "ナゴヤドーム": "Vantelin Dome Nagoya",
    "マツダスタジアム": "Mazda Stadium",
    "神　宮": "Meiji Jingu Stadium",
    "神宮": "Meiji Jingu Stadium",
    "明治神宮野球場": "Meiji Jingu Stadium",
    "楽天モバイル": "Rakuten Mobile Park Miyagi",
    "楽天モバイルパーク宮城": "Rakuten Mobile Park Miyagi",
    "エスコンＦ": "Es Con Field Hokkaido",
    "エスコンフィールド北海道": "Es Con Field Hokkaido",
    "ベルーナドーム": "Belluna Dome",
    "京セラＤ大阪": "Kyocera Dome Osaka",
    "京セラドーム大阪": "Kyocera Dome Osaka",
    "ＺＯＺＯマリン": "ZOZO Marine Stadium",
    "ゾゾマリン": "ZOZO Marine Stadium",
    "福岡ＰａｙＰａｙドーム": "Fukuoka PayPay Dome",
    "みずほＰａｙＰａｙドーム福岡": "Fukuoka PayPay Dome",
    # Quelques stades régionaux fréquemment utilisés pour des matchs "hors les murs"
    "盛　岡": "Morioka",
    "郡　山": "Koriyama",
    "沖縄セルラースタジアム那覇": "Okinawa Cellular Stadium Naha",
    "静　岡": "Shizuoka",
    "富　山": "Toyama",
}


def traduire_stade(nom_stade: str) -> str:
    """Traduit un nom de stade japonais vers l'anglais si connu, sinon le retourne tel quel."""
    if not nom_stade:
        return nom_stade
    return STADES_NPB.get(nom_stade, nom_stade)

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
def charger_urls_anglais_mensuel(annee: int, mois: int) -> dict:
    """
    Construit, pour un mois donné, un dictionnaire {(date, code_home, code_away): url}
    pointant vers la fiche de match EN ANGLAIS de npb.jp (section "/bis/eng/"), à
    partir de la page "All Teams Calendar" anglaise :
    https://npb.jp/bis/eng/{annee}/calendar/index_{mois}.html

    Cette page anglaise est la source utilisée pour récupérer les noms de joueurs en
    ROMAJI (alphabet latin) - la page japonaise (utilisée pour les runs/HR) n'affiche
    les noms qu'en kanji/kana. Mars et avril partagent la même page côté anglais
    ("index_04.html" couvre les deux mois), d'où le repli `mois_page`.
    """
    mois_page = 4 if mois == 3 else mois
    url = f"https://npb.jp/bis/eng/{annee}/calendar/index_{mois_page:02d}.html"
    resultat = {}
    try:
        soup = appeler_avec_retry(_get_soup, url)
    except Exception:
        return resultat

    for cellule in soup.select('td.stschedule'):
        lien_date = cellule.select_one('div.teschedate a[href]')
        if lien_date is None:
            continue
        m_date = re.search(r'gm(\d{4})(\d{2})(\d{2})\.html', lien_date['href'])
        if not m_date:
            continue
        date_str = f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}"

        for lien_match in cellule.select('div.stvsteam a[href]'):
            texte = lien_match.get_text(strip=True)
            m_equipes = re.match(r'^([A-Za-z]+)\s+(?:\d+|\*)\s*-\s*(?:\d+|\*)\s+([A-Za-z]+)$', texte)
            if not m_equipes:
                continue
            code_home, code_away = m_equipes.group(1).lower(), m_equipes.group(2).lower()
            href = lien_match['href']
            url_match = "https://npb.jp" + href if href.startswith('/') else href
            resultat[(date_str, code_home, code_away)] = url_match

    return resultat


@st.cache_data(show_spinner=False)
def _get_noms_romaji_match(url_anglais: str, est_domicile: bool):
    """
    Récupère, sur la fiche de match ANGLAISE npb.jp correspondante, la liste ordonnée
    des noms de famille des batteurs (en romaji) de l'équipe concernée, dans le MÊME
    ordre d'apparition que sur la page japonaise (ordre de frappe officiel) - ce qui
    permet, une fois les deux listes de même longueur, de faire correspondre chaque
    joueur ligne par ligne entre les deux langues sans avoir besoin de "deviner" une
    romanisation phonétique (peu fiable, en particulier pour les joueurs étrangers
    dont le nom est écrit en katakana, ex: ダルベック -> Dalbec, imprévisible par
    simple transcription phonétique).

    Détail technique npb.jp (eng) : les 2 tableaux de frappeurs partagent la classe
    CSS "gmtbltop" avec d'autres tableaux de mise en page (bannières d'équipe) ; on ne
    garde donc que ceux contenant des cellules "gmbatter"/"gmnxtbatter". Le premier
    tableau trouvé est celui de l'équipe à l'EXTÉRIEUR, le second celui à DOMICILE
    (même convention que côté japonais).
    """
    if not url_anglais:
        return []
    try:
        soup = appeler_avec_retry(_get_soup, url_anglais)
    except Exception:
        return []

    tables_frappeurs = [
        t for t in soup.select('table.gmtbltop')
        if t.select_one('td.gmbatter, td.gmnxtbatter')
    ]
    if len(tables_frappeurs) < 2:
        return []

    table = tables_frappeurs[1] if est_domicile else tables_frappeurs[0]
    noms = []
    for td in table.select('td.gmbatter, td.gmnxtbatter'):
        # Format de cellule : "Nom, POSTE" (ex: "Dalbec, 3B-1B") -> on ne garde que le nom
        nom = td.get_text(strip=True).split(',')[0].strip()
        noms.append(nom)
    return noms


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
                # Colonnes internes (non affichées) : nécessaires pour retrouver la
                # fiche de match anglaise (noms de joueurs en romaji) plus tard.
                "code_home": g['code_home'],
                "code_away": g['code_away'],
            })
        df = pd.DataFrame(matchs).sort_values('Date').reset_index(drop=True)
        return df
    except Exception as e:
        st.error(f"Erreur lors du chargement des données pour {equipe_abbr} ({annee}): {e}")
        return pd.DataFrame()


def _scraper_stats_offensives_match(box_url: str, est_domicile: bool, date_str: str = None,
                                     code_home: str = None, code_away: str = None):
    """
    Récupère, via le boxscore npb.jp d'un match (page japonaise détaillée, la seule à
    exposer les runs marqués par batteur), les runs ET les home runs marqués par
    chaque joueur de l'équipe (domicile ou extérieur) lors de ce match.
    Retourne une liste de dicts {'name': str, 'runs': int, 'hr': int}.

    Fonction "brute" (non mise en cache Streamlit ici) : c'est le CORPS PARTAGÉ par
    deux fonctions publiques mises en cache différemment selon leur usage -
    `get_stats_offensives_match` (cache permanent, pour les matchs déjà terminés de
    l'onglet "Analyse par Équipe") et `obtenir_hr_joueurs_match_jour` (cache à durée
    de vie limitée + invalidation à la demande, pour un match potentiellement EN COURS
    dans l'onglet "Résumé"). Voir leurs docstrings respectives.

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

    --- Noms de joueurs en ROMAJI (alphabet latin) ---
    La page japonaise ne donne les noms qu'en kanji/kana. Si `date_str`, `code_home`
    et `code_away` sont fournis, cette fonction va chercher la fiche de match
    ANGLAISE correspondante (`charger_urls_anglais_mensuel` + `_get_noms_romaji_match`)
    et, si elle liste EXACTEMENT le même nombre de batteurs dans le même ordre (ce qui
    est le cas en pratique : même ordre de passage au bâton des deux côtés), remplace
    les noms japonais par leur équivalent romaji officiel ligne par ligne - PAS de
    transcription phonétique automatique (peu fiable, notamment pour les joueurs
    étrangers en katakana), mais bien le nom romanisé publié par npb.jp lui-même.
    En cas d'échec (page indisponible, décompte différent), on retombe silencieusement
    sur les noms japonais plutôt que de planter l'affichage.
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

    lignes_ordonnees = []  # préserve l'ordre de frappe, nécessaire pour l'association avec les noms romaji
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

        lignes_ordonnees.append({'name': nom, 'runs': runs, 'hr': hr})

    # --- Substitution par les noms romaji officiels (si disponibles) ---
    if date_str and code_home and code_away:
        try:
            mois = int(date_str.split('-')[1])
            urls_anglais = charger_urls_anglais_mensuel(int(date_str.split('-')[0]), mois)
            url_anglais = urls_anglais.get((date_str, code_home, code_away))
            noms_romaji = _get_noms_romaji_match(url_anglais, est_domicile) if url_anglais else []
            if noms_romaji and len(noms_romaji) == len(lignes_ordonnees):
                for ligne, nom_romaji in zip(lignes_ordonnees, noms_romaji):
                    ligne['name'] = nom_romaji
        except Exception:
            pass  # on garde les noms japonais en repli, jamais d'erreur bloquante ici

    # Fusion des statistiques par nom final (romaji si dispo, japonais sinon), au cas
    # où un même joueur apparaîtrait sur plusieurs lignes (remplacement puis retour, etc.)
    stats_par_joueur = {}
    for ligne in lignes_ordonnees:
        if ligne['runs'] <= 0 and ligne['hr'] <= 0:
            continue
        cle = ligne['name']
        if cle not in stats_par_joueur:
            stats_par_joueur[cle] = {'runs': 0, 'hr': 0}
        stats_par_joueur[cle]['runs'] += ligne['runs']
        stats_par_joueur[cle]['hr'] += ligne['hr']

    return [{'name': nom, 'runs': s['runs'], 'hr': s['hr']} for nom, s in stats_par_joueur.items()]


@st.cache_data(show_spinner=False)
def get_stats_offensives_match(box_url: str, est_domicile: bool, date_str: str = None,
                                code_home: str = None, code_away: str = None):
    """
    Version mise en cache SANS expiration de `_scraper_stats_offensives_match`, utilisée
    par l'onglet "Analyse par Équipe" (`get_matchs_avec_scoreurs`) : les matchs concernés
    y sont toujours des matchs TERMINÉS (cf. `charger_donnees_equipe`), dont le boxscore
    ne changera plus jamais - un cache permanent est donc à la fois exact et évite de
    rescraper toute la saison à chaque rerun.
    """
    return _scraper_stats_offensives_match(box_url, est_domicile, date_str, code_home, code_away)


@st.cache_data(show_spinner=False, ttl=3600, max_entries=200)
def obtenir_hr_joueurs_match_jour(box_url: str, est_domicile: bool, date_str: str = None,
                                   code_home: str = None, code_away: str = None, cache_bust: int = 0):
    """
    Retourne uniquement les home runs (liste de tuples (nom_joueur, nb_hr)) d'une
    équipe pour UN match, à partir du même boxscore que `get_stats_offensives_match`.
    Fonction dédiée à l'onglet "Résumé" (plutôt que de réutiliser
    `get_stats_offensives_match`, jamais invalidée) car ici le match peut être EN
    COURS : `cache_bust` change la clé de cache Streamlit à la demande (incrémenté
    par le bouton "Rafraîchir"), ce qui permet de forcer un nouveau scraping sans
    dépendre d'un simple TTL. Le paramètre n'est jamais lu dans le corps de la
    fonction, il ne sert qu'à invalider le cache. `ttl=3600` reste un filet de
    sécurité pour éviter une croissance illimitée du cache, pas le mécanisme
    principal de fraîcheur des données.
    """
    try:
        stats = _scraper_stats_offensives_match(box_url, est_domicile, date_str, code_home, code_away)
    except Exception:
        # Ne doit jamais faire planter l'onglet Résumé : simplement pas de HR affiché.
        return []
    return [(s['name'], s['hr']) for s in stats if s.get('hr', 0) > 0]


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
        stats_batteurs = get_stats_offensives_match(
            ligne['box_url'],
            bool(ligne['Est_Domicile']),
            date_str=ligne.get('Date'),
            code_home=ligne.get('code_home'),
            code_away=ligne.get('code_away'),
        )

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
def _charger_ids_lanceurs_annonces():
    """
    Scrape la page officielle des lanceurs partants annoncés ("予告先発投手") :
    https://npb.jp/announcement/starter/

    Au Japon, les lanceurs partants sont annoncés la VEILLE pour le lendemain : cette
    page contient donc, en pratique, les partants du prochain jour de matchs - ce qui
    correspond exactement au "match du jour" (JST) recherché par cette application.

    On ne récupère ICI que l'identifiant npb.jp du joueur (le nom, lui, est ensuite
    récupéré en anglais/romaji via `obtenir_infos_lanceur`, qui utilise la fiche
    joueur anglaise plutôt que le nom japonais affiché sur cette page).

    Retourne un dict {code_equipe_minuscule: id_lanceur_npb}.
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
            if m_id:
                resultat[code] = m_id.group(1)

    return resultat


def obtenir_id_lanceur_annonce(code_equipe: str):
    """Retourne l'identifiant npb.jp du lanceur annoncé pour le code équipe donné, ou None."""
    if not code_equipe:
        return None
    return _charger_ids_lanceurs_annonces().get(code_equipe.lower())


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

    # Le lanceur annoncé est cherché sur la page dédiée "/announcement/starter/", qui
    # fournit l'identifiant npb.jp du joueur. Le NOM (en anglais/romaji) et les
    # statistiques du lanceur adverse sont ensuite récupérés EN UNE SEULE FOIS via
    # `obtenir_infos_lanceur` (fiche joueur anglaise), pour les deux équipes - ce qui
    # évite un second appel réseau séparé côté interface pour les stats du lanceur
    # adverse, et garantit que le nom affiché n'est jamais en kanji/kana.
    id_lanceur_notre_equipe = obtenir_id_lanceur_annonce(code_equipe)
    id_lanceur_adverse = obtenir_id_lanceur_annonce(code_adverse)

    infos_notre_lanceur = obtenir_infos_lanceur(id_lanceur_notre_equipe, ANNEE_COURANTE)
    infos_lanceur_adverse = obtenir_infos_lanceur(id_lanceur_adverse, ANNEE_COURANTE)

    lanceur_notre_equipe = infos_notre_lanceur['nom'] if infos_notre_lanceur else None
    lanceur_adverse = infos_lanceur_adverse['nom'] if infos_lanceur_adverse else None

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
        # Stats du lanceur de NOTRE équipe, récupérées EXACTEMENT de la même façon
        # (symétrique) que celles du lanceur adverse ci-dessous (voir `infos_notre_lanceur`
        # plus haut) : nécessaires au module "Probabilité de Victoire" ci-dessous, qui
        # compare les DEUX lanceurs partants annoncés.
        'stats_lanceur_nous': infos_notre_lanceur,
        'stats_lanceur_adverse': infos_lanceur_adverse,
        'heure_jst': heure_jst_str or "—",
        'heure_paris': heure_paris_str or "—",
        'statut': statut,
        'venue': traduire_stade(ligne.get('lieu')) or "—",
    }


@st.cache_data(show_spinner=False, ttl=3600)
def obtenir_infos_lanceur(id_lanceur: str, annee: int):
    """
    Récupère, via la fiche joueur ANGLAISE officielle npb.jp
    (https://npb.jp/bis/eng/players/{id}.html), le nom romanisé du lanceur ET ses
    statistiques de la saison en cours (ERA, WHIP calculé, runs/HR alloués, HR/9,
    apparitions). Utiliser la page anglaise (plutôt que la page japonaise utilisée
    dans une version précédente) donne directement le nom en alphabet latin, sans
    avoir besoin d'une étape de traduction séparée.

    NPB.jp n'affiche pas de WHIP ni de HR/9 tout faits sur cette page (contrairement à
    MLB StatsAPI) : ils sont donc calculés ici à partir des statistiques brutes
    publiées (H=hits alloués, BB=buts-sur-balles alloués, IP=manches lancées,
    HR=home runs alloués).

    Retourne un dict {'nom', 'era', 'whip', 'runs_alloues', 'hr_alloues', 'hr_par_9',
    'matchs_titulaire'} dès que la fiche joueur existe (le nom est alors toujours
    renseigné), avec les champs statistiques à None si aucune ligne de stats n'existe
    pour `annee` (ex: lanceur tout juste appelé, sans historique exploitable).
    Retourne None si l'id est vide ou si la fiche joueur est introuvable.
    """
    if not id_lanceur:
        return None
    url = f"https://npb.jp/bis/eng/players/{id_lanceur}.html"
    try:
        soup = appeler_avec_retry(_get_soup, url)
    except Exception:
        return None

    li_nom = soup.find('li', id='pc_v_name')
    nom = li_nom.get_text(strip=True) if li_nom else None
    if not nom:
        return None

    resultat = {
        'nom': nom,
        'era': None,
        'whip': None,
        'runs_alloues': None,
        'hr_alloues': None,
        'hr_par_9': None,
        'matchs_titulaire': None,
    }

    table = soup.find('table', id='tablefix_p')
    if table is None:
        return resultat
    tbody = table.find('tbody')
    if tbody is None:
        return resultat

    ligne_annee = None
    for tr in tbody.find_all('tr'):
        td_annee = tr.find('td', class_='year')
        if td_annee and td_annee.get_text(strip=True) == str(annee):
            ligne_annee = tr
    if ligne_annee is None:
        return resultat

    tds = ligne_annee.find_all('td', recursive=False)
    # Colonnes (page ANGLAISE - son ordre diffère légèrement de la page japonaise, qui
    # a une colonne supplémentaire "無四球"/matchs sans BB entre CG/SHO et PCT) :
    # 0:Year 1:Team 2:G(apparitions) 3:W 4:L 5:SV 6:HLD 7:HP 8:CG 9:SHO 10:PCT 11:BF
    # 12:IP(tableau imbriqué) 13:H 14:HR 15:BB 16:HB 17:SO 18:WP 19:BK 20:R 21:ER 22:ERA
    if len(tds) < 23:
        return resultat

    def _txt(i):
        return tds[i].get_text(strip=True)

    try:
        apparitions = int(_txt(2) or 0)
        hits_alloues = int(_txt(13) or 0)
        hr_alloues = int(_txt(14) or 0)
        bb_alloues = int(_txt(15) or 0)
        runs_alloues = int(_txt(20) or 0)
        era = float(_txt(22) or 0)
    except ValueError:
        return resultat

    if not era:
        return resultat

    # Manches lancées : encodées dans un mini-tableau imbriqué à l'intérieur de la
    # cellule "IP" (entier de manches en <th>, fraction de manche ".1"/".2" en <td>).
    manches_entieres, tiers = 0, 0
    cellule_ip = tds[12]
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
        return resultat

    resultat.update({
        'era': era,
        'whip': (hits_alloues + bb_alloues) / innings_lancees,
        'runs_alloues': runs_alloues,
        'hr_alloues': hr_alloues,
        'hr_par_9': (hr_alloues / innings_lancees) * 9,
        'matchs_titulaire': apparitions,
    })
    return resultat


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

    if stats_lanceur_adverse is not None and (stats_lanceur_adverse.get('era') or 0) > 0:
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


def predire_probabilite_victoire(
    moyenne_runs_nous,
    moyenne_offense_adverse,
    stats_lanceur_nous,
    stats_lanceur_adverse,
    est_domicile: bool,
):
    """
    Estimation heuristique (PAS un modèle statistique validé - aucune régression logistique
    entraînée sur des données historiques ici, juste une pondération "de bon sens") de la
    probabilité de victoire de l'équipe sélectionnée ("nous") face à son adversaire du jour,
    exprimée en pourcentage pour CHAQUE équipe (les deux valeurs retournées somment à 100%).

    --- Les 3 facteurs retenus, et leur pondération ---
    1. LANCEURS PARTANTS ANNONCÉS (poids 60% dans le score combiné - facteur jugé le PLUS
       déterminant, comme demandé : à l'échelle d'UN match de baseball, un lanceur partant
       influence directement 5 à 7 manches sur 9, un poids qu'aucun frappeur isolé n'a à
       lui seul). Pour chaque lanceur, on calcule un "indice de qualité" =
       (1/ERA) * 0.7 + (1/WHIP) * 0.3 : l'ERA pèse plus car c'est la statistique la plus
       lisible/suivie, le WHIP vient l'affiner (il capture aussi les coureurs laissés sur
       les buts, pas seulement les points encaissés). Plus l'indice est élevé (ERA/WHIP
       BAS), plus la probabilité penche vers l'équipe de ce lanceur. La part de chaque
       équipe dans ce facteur est simplement son indice rapporté à la somme des deux
       indices (ex: si notre lanceur a un indice deux fois plus élevé que l'adverse, on
       obtient 2/3 - 1/3, PAS 100% - 0%, pour rester réaliste).
    2. DYNAMIQUE OFFENSIVE RÉCENTE (poids 40% dans le score combiné) : moyenne de runs
       marqués sur les 10 derniers matchs de CHAQUE équipe. Pour notre équipe, on réutilise
       directement `moyenne_runs_10` (déjà calculé ailleurs dans l'onglet). Pour l'attaque
       ADVERSE, faute de recharger séparément ses 10 derniers matchs (appel réseau
       supplémentaire non indispensable dans le temps imparti), on réutilise EXACTEMENT le
       même proxy que `predire_runs_match` juste au-dessus : la moyenne de runs CONCÉDÉS
       par NOTRE équipe sur ses 10 derniers matchs (`moyenne_ra_10`), un indicateur
       indirect mais raisonnable de la force offensive à laquelle notre équipe a été
       récemment confrontée. Ce choix est documenté ici explicitement plutôt que caché.
    3. AVANTAGE DU TERRAIN (bonus fixe de +3 points de pourcentage, PAS un facteur pondéré
       avec les deux précédents - appliqué APRÈS le score combiné) pour l'équipe qui reçoit.
       Valeur choisie par prudence : les études sabermétriques MLB situent le taux de
       victoires à domicile autour de 53-54% en moyenne sur longue période (soit un
       avantage net d'environ 3 à 4 points par rapport à un match parfaitement équilibré à
       50/50) ; on retient ici la borne basse (+3) faute d'étude équivalente publiée
       spécifiquement sur la NPB, pour ne pas sur-pondérer un facteur secondaire.

    --- Dégradation gracieuse (données manquantes) ---
    - Lanceur sans ERA exploitable (`stats_lanceur_nous`/`stats_lanceur_adverse` vaut None,
      ou n'a pas de champ 'era' renseigné - cas fréquent en NPB pour un lanceur sans
      historique cette saison, ex: recrue tout juste appelée ou joueur étranger fraîchement
      arrivé) : ce lanceur reçoit un ERA/WHIP "neutres" (`ERA_NEUTRE`/`WHIP_NEUTRE`, des
      moyennes de ligue approximatives), ce qui revient à neutraliser sa contribution
      individuelle SANS jamais planter ni fausser l'estimation vers un 0%/100% trompeur.
      Si les DEUX lanceurs manquent, le facteur 1 devient entièrement neutre (50/50), et
      seuls les facteurs 2 et 3 continuent à jouer.
    - Moyenne de runs manquante (`None`/`NaN`, ex: moins de 10 matchs joués cette saison) :
      remplacée par une moyenne "neutre" (`RUNS_NEUTRE`), pour la même raison.
    - Aucune combinaison de données manquantes ne peut faire planter cette fonction : au
      pire (aucune donnée du tout), elle retombe sur un 50/50 + bonus domicile.

    Retourne un tuple (pct_nous, pct_adverse) de deux flottants arrondis à 1 décimale dont
    la SOMME vaut exactement 100.0, chacun bornée entre 5.0 et 95.0 : une simple heuristique
    ne doit jamais afficher une fausse "certitude absolue" à 0% ou 100%.
    """
    # Valeurs "neutres" de repli (moyennes de ligue approximatives), utilisées uniquement
    # quand une donnée réelle manque, pour neutraliser proprement le facteur concerné.
    ERA_NEUTRE = 4.50    # ERA moyen approximatif toutes équipes NPB confondues
    WHIP_NEUTRE = 1.35   # WHIP moyen approximatif toutes équipes NPB confondues
    RUNS_NEUTRE = 4.50   # Runs/match moyens approximatifs en NPB
    BONUS_DOMICILE = 3.0  # Points de pourcentage (voir justification ci-dessus)

    def _indice_qualite_lanceur(stats_lanceur):
        """Indice de qualité d'un lanceur (plus haut = meilleur), avec repli neutre."""
        if stats_lanceur is not None and stats_lanceur.get('era'):
            era = stats_lanceur['era']
            whip = stats_lanceur.get('whip') or WHIP_NEUTRE
        else:
            era, whip = ERA_NEUTRE, WHIP_NEUTRE
        return (1.0 / era) * 0.7 + (1.0 / whip) * 0.3

    # --- Facteur 1 : lanceurs partants (poids 60%) ---
    qualite_nous = _indice_qualite_lanceur(stats_lanceur_nous)
    qualite_adverse = _indice_qualite_lanceur(stats_lanceur_adverse)
    part_lanceurs_nous = qualite_nous / (qualite_nous + qualite_adverse)

    # --- Facteur 2 : dynamique offensive récente (poids 40%) ---
    runs_nous = (
        moyenne_runs_nous if moyenne_runs_nous is not None and pd.notna(moyenne_runs_nous)
        else RUNS_NEUTRE
    )
    runs_adverse = (
        moyenne_offense_adverse if moyenne_offense_adverse is not None and pd.notna(moyenne_offense_adverse)
        else RUNS_NEUTRE
    )
    somme_runs = runs_nous + runs_adverse
    part_offense_nous = (runs_nous / somme_runs) if somme_runs > 0 else 0.5

    # --- Score combiné (facteurs 1 + 2), puis conversion en pourcentage ---
    part_combinee_nous = (part_lanceurs_nous * 0.6) + (part_offense_nous * 0.4)
    pct_nous = part_combinee_nous * 100.0

    # --- Facteur 3 : avantage du terrain (bonus fixe, appliqué après coup) ---
    pct_nous += BONUS_DOMICILE if est_domicile else -BONUS_DOMICILE

    # Bornes de sécurité (jamais 0%/100% avec une simple heuristique) + normalisation
    # stricte à 100% (l'adversaire récupère exactement le complément).
    pct_nous = max(5.0, min(95.0, pct_nous))
    pct_adverse = 100.0 - pct_nous

    return round(pct_nous, 1), round(pct_adverse, 1)


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
    if stats_lanceur_adverse is not None and (stats_lanceur_adverse.get('era') or 0) > 0:
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


def _normaliser_colonne(serie: pd.Series) -> pd.Series:
    """
    Normalisation min-max dans [0, 1] d'une colonne de statistiques, pour pouvoir
    combiner des métriques d'échelles très différentes (ex: HR 0-6, ERA 2-6) dans un
    même indice pondéré. Renvoie une série neutre à 0.5 si la colonne est constante
    (évite une division par zéro sans fausser le classement).
    """
    minimum, maximum = serie.min(), serie.max()
    if pd.isna(minimum) or pd.isna(maximum) or maximum == minimum:
        return pd.Series([0.5] * len(serie), index=serie.index)
    return (serie - minimum) / (maximum - minimum)


@st.cache_data(show_spinner=False, ttl=1800)
def obtenir_resume_10_derniers_matchs_equipe(annee: int, code_equipe: str):
    """
    Version "légère" de `get_matchs_avec_scoreurs`, dédiée à l'onglet Hot Pronostics :
    ne scrape les boxscores QUE des 10 DERNIERS matchs terminés de l'équipe (au lieu de
    toute la saison). L'onglet Hot Pronostics a besoin de ce résumé pour TOUTES les
    équipes qui jouent aujourd'hui (jusqu'à 12) : rescraper la saison complète de
    chacune (comme le fait l'onglet "Analyse par Équipe" pour l'équipe sélectionnée)
    multiplierait le nombre de requêtes envoyées à npb.jp par (matchs de la saison) x
    (équipes du jour), un coût vite prohibitif en pleine saison régulière. Chaque
    boxscore individuel reste mis en cache par `get_stats_offensives_match`, donc
    consulter ensuite l'onglet "Analyse par Équipe" pour une de ces équipes réutilise
    directement les 10 boxscores déjà récupérés ici.

    Retourne un dict {'moyenne_runs', 'moyenne_ra', 'cumul_runs', 'cumul_hr'} (mêmes
    clés que celles produites par `calculer_resume_10_derniers_matchs`), ou None si
    aucune donnée n'est disponible pour cette équipe/saison.
    """
    df_equipe = charger_donnees_equipe(annee, code_equipe)
    if df_equipe.empty:
        return None

    df_10 = df_equipe.tail(10).copy()
    df_10['_offensive_stats'] = [
        get_stats_offensives_match(
            ligne['box_url'],
            bool(ligne['Est_Domicile']),
            date_str=ligne.get('Date'),
            code_home=ligne.get('code_home'),
            code_away=ligne.get('code_away'),
        )
        for _, ligne in df_10.iterrows()
    ]

    moyenne_runs, _, _, _, cumul_runs, cumul_hr = calculer_resume_10_derniers_matchs(df_10)
    moyenne_ra = pd.to_numeric(df_10['RA'], errors='coerce').mean() if 'RA' in df_10.columns else None

    return {
        'moyenne_runs': moyenne_runs,
        'moyenne_ra': moyenne_ra,
        'cumul_runs': cumul_runs,
        'cumul_hr': cumul_hr,
    }


def _calculer_top5_home_runs_npb(candidats: list) -> pd.DataFrame:
    """
    Construit le classement "Top 5 Home Runs probables" à partir de la liste de
    candidats (un par joueur ayant marqué au moins 1 HR sur les 10 derniers matchs de
    son équipe, pour chaque équipe jouant aujourd'hui). Indice pondéré : HR sur les 10
    derniers matchs (60%) + HR/9 du lanceur partant adverse ANNONCÉ (40%).

    npb.jp ne fournissant pas de SLG par joueur (contrairement à MLB StatsAPI, qui
    l'expose via un endpoint groupé), ce 3e facteur utilisé côté MLB est absent ici
    (voir docstring de `construire_donnees_hot_pronostics` pour le détail des
    différences par rapport à la version MLB).
    """
    if not candidats:
        return pd.DataFrame()
    df = pd.DataFrame(candidats)
    indice = (
        _normaliser_colonne(df['HR (10 derniers matchs)']) * 0.60
        + _normaliser_colonne(df['HR/9 lanceur adverse']) * 0.40
    ) * 100
    df['Indice HR (/100)'] = indice.round(1)
    df = df.sort_values('Indice HR (/100)', ascending=False).head(5).reset_index(drop=True)
    return df[[
        'Joueur', 'Équipe', 'Adversaire', 'Lanceur adverse',
        'HR (10 derniers matchs)', 'HR/9 lanceur adverse', 'Indice HR (/100)'
    ]]


def _calculer_top5_runs_npb(candidats: list) -> pd.DataFrame:
    """
    Construit le classement "Top 5 joueurs pour marquer un run" à partir de la liste de
    candidats (un par joueur ayant marqué au moins 1 run sur les 10 derniers matchs de
    son équipe, pour chaque équipe jouant aujourd'hui). Indice pondéré : runs sur les
    10 derniers matchs (60%) + ERA du lanceur partant adverse ANNONCÉ (40%, un ERA
    élevé indique un lanceur plus "battable").

    npb.jp ne fournissant ni l'OBP par joueur ni la position dans le lineup (les
    lineups ne sont, contrairement à MLB, pas publiées à l'avance), ces deux facteurs
    utilisés côté MLB sont absents ici (voir docstring de
    `construire_donnees_hot_pronostics`).
    """
    if not candidats:
        return pd.DataFrame()
    df = pd.DataFrame(candidats)
    indice = (
        _normaliser_colonne(df['Runs (10 derniers matchs)']) * 0.60
        + _normaliser_colonne(df['ERA lanceur adverse']) * 0.40
    ) * 100
    df['Indice Run (/100)'] = indice.round(1)
    df = df.sort_values('Indice Run (/100)', ascending=False).head(5).reset_index(drop=True)
    return df[[
        'Joueur', 'Équipe', 'Adversaire', 'Lanceur adverse',
        'Runs (10 derniers matchs)', 'ERA lanceur adverse', 'Indice Run (/100)'
    ]]


def _total_runs_predit(resume_home, resume_away):
    """
    Total de runs projeté pour un match = somme des moyennes de runs marqués par
    chaque équipe sur ses 10 derniers matchs. Sert de projection "Over/Under" dans le
    bilan des prédictions de la veille (cf. `obtenir_ligne_over_under_saison`).
    Retourne None si l'une des deux moyennes n'est pas disponible.
    """
    if not resume_home or not resume_away:
        return None
    moyenne_home = resume_home.get('moyenne_runs')
    moyenne_away = resume_away.get('moyenne_runs')
    if moyenne_home is None or moyenne_away is None or pd.isna(moyenne_home) or pd.isna(moyenne_away):
        return None
    return round(float(moyenne_home) + float(moyenne_away), 2)


def _top_candidats_hr(resume_camp, n: int = 2) -> list:
    """Les `n` joueurs les plus en forme au HR (10 derniers matchs) d'une équipe."""
    if not resume_camp or not resume_camp.get('cumul_hr'):
        return []
    return [nom for nom, _ in sorted(resume_camp['cumul_hr'].items(), key=lambda x: x[1], reverse=True)[:n]]


@st.cache_data(show_spinner=False, ttl=1800)
def construire_donnees_hot_pronostics(annee: int):
    """
    Calcul GLOBAL et coûteux (mis en cache via @st.cache_data, ttl=30min) qui scanne
    TOUS les matchs du jour (heure du Japon, JST) et construit les 3 tableaux de
    l'onglet "Hot Pronostics" : Top 5 Home Runs, Top 5 joueurs pour marquer un run, et
    le récapitulatif Win/Lose de chaque confrontation. Ce calcul est indépendant de
    l'équipe sélectionnée dans la sidebar, donc mis en cache séparément (clé = `annee`
    uniquement) pour ne jamais être relancé inutilement quand l'utilisateur change
    d'équipe.

    --- Différences par rapport à la version MLB (StatsAPI) ---
    Cet onglet est un portage adapté de l'onglet équivalent de MLB_Stats_App, qui
    s'appuie sur des capacités propres à MLB StatsAPI absentes côté npb.jp (scraping) :
    - Pas de lineups officielles (ordre de frappe) publiées à l'avance : les candidats
      HR/Runs ne sont donc pas les titulaires confirmés du jour (inconnus à l'avance
      en NPB), mais les joueurs de chaque équipe qui jouent aujourd'hui ayant marqué
      des runs/HR sur leurs 10 DERNIERS matchs (même source de vérité que le résumé
      "10 derniers matchs" de l'onglet Analyse par Équipe) - une bonne mesure de forme
      récente, déjà utilisée et validée ailleurs dans l'application.
    - Pas d'endpoint groupé de stats saison par joueur (SLG/OBP) : les indices HR/Runs
      n'utilisent donc que 2 facteurs chacun (voir `_calculer_top5_home_runs_npb` /
      `_calculer_top5_runs_npb`) au lieu de 3 côté MLB.
    - Les lanceurs partants ANNONCÉS (une seule page pour tous les matchs du jour, cf.
      `_charger_ids_lanceurs_annonces`) et le modèle `predire_probabilite_victoire`
      (déjà utilisé par l'onglet "Prédictions du jour") sont en revanche réutilisés
      TELS QUELS, exactement comme côté MLB.

    Retourne (matchs_du_jour, df_top5_hr, df_top5_runs, df_victoires).
    """
    df_jour, maintenant_jst = obtenir_calendrier_du_jour_jst()
    if df_jour.empty:
        return [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    ids_lanceurs_annonces = _charger_ids_lanceurs_annonces()

    matchs_du_jour = []
    for _, g in df_jour.iterrows():
        code_home = (g.get('code_home') or '').lower()
        code_away = (g.get('code_away') or '').lower()
        if not code_home or not code_away:
            continue
        nom_home = TEAMS_NPB.get(code_home.upper(), g.get('nom_home'))
        nom_away = TEAMS_NPB.get(code_away.upper(), g.get('nom_away'))

        infos_p_home = obtenir_infos_lanceur(ids_lanceurs_annonces.get(code_home), annee)
        infos_p_away = obtenir_infos_lanceur(ids_lanceurs_annonces.get(code_away), annee)

        # Double fuseau horaire (même logique que `obtenir_match_du_jour`)
        heure_jst_str = (g.get('heure_jst') or '').strip()
        heure_paris_str = None
        if re.match(r'^\d{1,2}:\d{2}$', heure_jst_str):
            try:
                h, m = map(int, heure_jst_str.split(':'))
                dt_jst = datetime(maintenant_jst.year, maintenant_jst.month, maintenant_jst.day, h, m, tzinfo=TZ_JST)
                heure_paris_str = dt_jst.astimezone(TZ_PARIS).strftime('%d/%m à %H:%M')
            except Exception:
                heure_paris_str = None

        matchs_du_jour.append({
            'code_home': code_home,
            'code_away': code_away,
            'home_name': nom_home,
            'away_name': nom_away,
            'home_pitcher': infos_p_home,
            'away_pitcher': infos_p_away,
            'heure_jst': heure_jst_str or "—",
            'heure_paris': heure_paris_str or "—",
        })

    if not matchs_du_jour:
        return [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # Résumé "10 derniers matchs" calculé UNE SEULE FOIS par équipe jouant aujourd'hui
    # (une équipe NPB ne joue qu'un seul match par jour, donc pas de risque de calcul
    # en double ici).
    codes_equipes_jour = sorted({m['code_home'] for m in matchs_du_jour} | {m['code_away'] for m in matchs_du_jour})
    resumes_equipe = {
        code: obtenir_resume_10_derniers_matchs_equipe(annee, code.upper())
        for code in codes_equipes_jour
    }

    candidats_hr = []
    candidats_runs = []
    lignes_victoire = []

    for m in matchs_du_jour:
        resume_home = resumes_equipe.get(m['code_home'])
        resume_away = resumes_equipe.get(m['code_away'])

        # --- Tableau Win/Lose : on réutilise TEL QUEL le modèle heuristique déjà
        # validé dans l'onglet "Prédictions du jour" (`predire_probabilite_victoire`),
        # avec les moyennes de runs RÉELLES des deux équipes (au lieu du proxy "runs
        # concédés par notre équipe" utilisé dans l'onglet mono-équipe, où l'attaque
        # adverse n'est pas directement disponible sans ce résumé multi-équipes).
        pct_home, pct_away = predire_probabilite_victoire(
            resume_home['moyenne_runs'] if resume_home else None,
            resume_away['moyenne_runs'] if resume_away else None,
            m['home_pitcher'],
            m['away_pitcher'],
            est_domicile=True,
        )
        lignes_victoire.append({
            'Heure (France)': m['heure_paris'],
            'Équipe Domicile': m['home_name'],
            'Lanceur Domicile': (m['home_pitcher']['nom'] if m['home_pitcher'] else None) or 'Non annoncé',
            'Équipe Extérieur': m['away_name'],
            'Lanceur Extérieur': (m['away_pitcher']['nom'] if m['away_pitcher'] else None) or 'Non annoncé',
            'Proba Domicile (%)': pct_home,
            'Proba Extérieur (%)': pct_away,
        })

        # --- Candidats HR / Runs : chaque équipe est croisée avec le lanceur partant
        # ADVERSE annoncé (celui qu'elle affrontera aujourd'hui).
        for resume_camp, lanceur_adverse, equipe_nom, adversaire_nom in (
            (resume_home, m['away_pitcher'], m['home_name'], m['away_name']),
            (resume_away, m['home_pitcher'], m['away_name'], m['home_name']),
        ):
            if not resume_camp:
                continue
            nom_lanceur_adverse = lanceur_adverse['nom'] if lanceur_adverse else 'Non annoncé'
            hr9_adverse = (
                lanceur_adverse['hr_par_9']
                if lanceur_adverse and lanceur_adverse.get('hr_par_9') else 1.0
            )
            era_adverse = (
                lanceur_adverse['era']
                if lanceur_adverse and lanceur_adverse.get('era') else 4.5
            )

            for nom_joueur, total_hr in resume_camp['cumul_hr'].items():
                candidats_hr.append({
                    'Joueur': nom_joueur,
                    'Équipe': equipe_nom,
                    'Adversaire': adversaire_nom,
                    'Lanceur adverse': nom_lanceur_adverse,
                    'HR (10 derniers matchs)': total_hr,
                    'HR/9 lanceur adverse': hr9_adverse,
                })
            for nom_joueur, total_runs in resume_camp['cumul_runs'].items():
                candidats_runs.append({
                    'Joueur': nom_joueur,
                    'Équipe': equipe_nom,
                    'Adversaire': adversaire_nom,
                    'Lanceur adverse': nom_lanceur_adverse,
                    'Runs (10 derniers matchs)': total_runs,
                    'ERA lanceur adverse': era_adverse,
                })

    df_top5_hr = _calculer_top5_home_runs_npb(candidats_hr)
    df_top5_runs = _calculer_top5_runs_npb(candidats_runs)
    df_victoires = pd.DataFrame(lignes_victoire)

    # --- Archivage de l'instantané du jour (pour le "Bilan des Prédictions" de la
    # veille, onglet Résumé, cf. `_sauvegarder_predictions_du_jour`) : on ne conserve
    # que ce qui est nécessaire à une comparaison ultérieure avec le résultat réel une
    # fois le match terminé (probabilité de victoire, total de runs projeté pour les
    # deux équipes, et candidats HR les plus en forme de chaque équipe).
    matches_snapshot = [
        {
            'code_home': m['code_home'],
            'code_away': m['code_away'],
            'home_name': m['home_name'],
            'away_name': m['away_name'],
            'proba_home': ligne_victoire.get('Proba Domicile (%)'),
            'proba_away': ligne_victoire.get('Proba Extérieur (%)'),
            'total_runs_predit': _total_runs_predit(
                resumes_equipe.get(m['code_home']), resumes_equipe.get(m['code_away'])
            ),
            'candidats_hr_home': _top_candidats_hr(resumes_equipe.get(m['code_home'])),
            'candidats_hr_away': _top_candidats_hr(resumes_equipe.get(m['code_away'])),
        }
        for m, ligne_victoire in zip(matchs_du_jour, lignes_victoire)
    ]
    _sauvegarder_predictions_du_jour(maintenant_jst.strftime('%Y-%m-%d'), matches_snapshot)

    return matchs_du_jour, df_top5_hr, df_top5_runs, df_victoires


# ============================================================
# ONGLET "RÉSUMÉ" - portage à l'identique de l'onglet équivalent de MLB_Stats_App
# ============================================================
# npb.jp (scraping) n'expose pas de statut de match "en direct" détaillé comme MLB
# StatsAPI (pas de manche/inning en cours, pas de statut "Warmup"/"Delayed" etc.) :
# la page de calendrier ne donne que le score courant (dès que le match a commencé)
# et, une fois le match terminé, le nom du lanceur gagnant/perdant. Le statut est donc
# ici forcément plus grossier ("Terminé" / "En cours" / "À venir", sans détail de
# manche) que côté MLB - c'est la seule différence fonctionnelle avec la version MLB,
# le reste (bouton de rafraîchissement, colonnes du tableau, comparatif avec l'algo
# de prédiction) est repris à l'identique.

def _formater_statut_match_npb(score_home, score_away, lanceur_gagnant: str, lanceur_perdant: str) -> str:
    """
    Détermine le statut d'un match NPB à partir des colonnes déjà scrapées par
    `charger_calendrier_mensuel` (mêmes règles que celles déjà utilisées par
    `obtenir_match_du_jour` pour le match de l'équipe sélectionnée, généralisées ici à
    N'IMPORTE QUEL match du jour) : "Terminé" si une décision (lanceur gagnant/perdant)
    a déjà été publiée, "En cours" si un score est déjà affiché mais sans décision
    encore publiée, "À venir" si aucun score n'est encore affiché.
    """
    if pd.notna(score_home) and pd.notna(score_away):
        if (lanceur_gagnant or '').strip() or (lanceur_perdant or '').strip():
            return "Terminé"
        return "En cours"
    return "À venir"


def _formater_segment_hr(abbr: str, hr_liste: list) -> str:
    """Formate les HR d'UNE équipe : 'G: 2 (Okamoto, Sanchez)' ou 'G: 0' si aucun HR."""
    total = sum(hr for _, hr in hr_liste)
    if total <= 0:
        return f"{abbr}: 0"
    noms = [nom if hr <= 1 else f"{nom} x{hr}" for nom, hr in hr_liste]
    return f"{abbr}: {total} ({', '.join(noms)})"


def _formater_cellule_hr(away_abbr: str, hr_away: list, home_abbr: str, hr_home: list) -> str:
    """Combine les HR des deux équipes d'un match dans une seule cellule de tableau."""
    return f"{_formater_segment_hr(away_abbr, hr_away)} | {_formater_segment_hr(home_abbr, hr_home)}"


def _comparer_prediction_vs_score(pred, home_nick: str, away_nick: str, home_score: int, away_score: int, a_commence: bool):
    """
    Retourne (texte_comparatif, icone_resultat) pour la colonne "Résultat vs Algo".
    - `pred` : ligne (pandas Series) issue de `df_victoires` (Hot Pronostics) pour ce
      match, ou None si aucune prédiction n'est encore disponible (lanceurs partants
      pas encore annoncés) -> ("Non disponible", "⏳").
    - Sinon : l'équipe favorite est celle avec la probabilité de victoire la plus
      haute. On compare cette équipe favorite à l'équipe actuellement en tête (ou
      gagnante si le match est terminé) : ✅ si elle mène/a gagné, ❌ si elle est
      menée/a perdu, ⏳ si le match n'a pas commencé ou si le score est à égalité.
    """
    if pred is None:
        return "Non disponible", "⏳"

    pct_home = pred.get('Proba Domicile (%)')
    pct_away = pred.get('Proba Extérieur (%)')
    if pct_home is None or pct_away is None or pd.isna(pct_home) or pd.isna(pct_away):
        return "Non disponible", "⏳"

    equipe_favorite = home_nick if pct_home >= pct_away else away_nick
    pct_favori = max(pct_home, pct_away)
    comparatif = f"{equipe_favorite} à {pct_favori:.0f}%"

    if not a_commence or home_score == away_score:
        return comparatif, "⏳"

    equipe_en_tete = home_nick if home_score > away_score else away_nick
    icone = "✅" if equipe_en_tete == equipe_favorite else "❌"
    return comparatif, icone


@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def construire_resume_matchs_du_jour(annee: int, cache_bust: int = 0):
    """
    Construit le tableau récapitulatif de TOUS les matchs NPB du jour (à venir, en
    cours, terminés - heure du Japon) pour l'onglet "Résumé". `cache_bust` sert
    uniquement à invalider le cache Streamlit à la demande (bouton "Rafraîchir les
    scores en direct") - le calcul du modèle de prédiction ("Hot Pronostics") n'est
    PAS reproduit à chaque rafraîchissement (il a son propre cache à `ttl=1800`, car il
    ne change pas au fil du match), seuls les scores/statuts/HR en direct sont
    re-récupérés.

    Retourne (DataFrame, message_erreur). En cas d'échec réseau, le DataFrame est vide
    et `message_erreur` contient un texte à afficher via `st.error` - aucune exception
    ne remonte jamais à l'appelant (l'application ne doit jamais planter à cause d'un
    appel de scraping en direct).
    """
    if annee != ANNEE_COURANTE:
        return pd.DataFrame(), None

    try:
        df_jour, _ = obtenir_calendrier_du_jour_jst()
    except Exception as e:
        return pd.DataFrame(), (
            f"Impossible de récupérer les scores en direct pour le moment ({e}). "
            "Réessayez dans quelques instants avec le bouton de rafraîchissement."
        )

    if df_jour.empty:
        return pd.DataFrame(), None

    # Prédictions déjà calculées pour "Hot Pronostics" (même modèle, même journée),
    # réutilisées ici pour la colonne "Comparatif Prédiction" - alignées par
    # (code_home, code_away) : une équipe NPB ne joue qu'un seul match par jour, cette
    # paire suffit donc à identifier chaque match sans ambiguïté (pas de "game_id"
    # exposé par npb.jp comme c'est le cas avec MLB StatsAPI).
    try:
        matchs_lineups, _, _, df_victoires = construire_donnees_hot_pronostics(annee)
    except Exception:
        matchs_lineups, df_victoires = [], pd.DataFrame()

    predictions_par_match = {}
    for idx, m in enumerate(matchs_lineups):
        if idx < len(df_victoires):
            predictions_par_match[(m.get('code_home'), m.get('code_away'))] = df_victoires.iloc[idx]

    lignes = []
    for _, g in df_jour.iterrows():
        code_home = (g.get('code_home') or '').lower()
        code_away = (g.get('code_away') or '').lower()
        if not code_home or not code_away:
            continue

        nom_home = TEAMS_NPB.get(code_home.upper(), g.get('nom_home') or '?')
        nom_away = TEAMS_NPB.get(code_away.upper(), g.get('nom_away') or '?')
        home_abbr = code_home.upper()
        away_abbr = code_away.upper()

        score_home, score_away = g.get('score_home'), g.get('score_away')
        statut_str = _formater_statut_match_npb(
            score_home, score_away, g.get('lanceur_gagnant'), g.get('lanceur_perdant')
        )
        a_commence = statut_str in ("Terminé", "En cours")

        try:
            home_score = int(score_home) if pd.notna(score_home) else 0
            away_score = int(score_away) if pd.notna(score_away) else 0
        except (TypeError, ValueError):
            home_score, away_score = 0, 0

        if a_commence:
            score_str = f"{away_abbr} {away_score} - {home_abbr} {home_score}"
            # Colonne texte (pas numérique) volontairement : elle doit pouvoir afficher
            # "—" pour les matchs pas encore commencés sans faire planter la
            # sérialisation Arrow du tableau (colonne à types mixtes int/str sinon).
            total_runs = str(home_score + away_score)
            box_url = g.get('box_url')
            date_str = g.get('Date')
            hr_home = obtenir_hr_joueurs_match_jour(box_url, True, date_str, code_home, code_away, cache_bust)
            hr_away = obtenir_hr_joueurs_match_jour(box_url, False, date_str, code_home, code_away, cache_bust)
            hr_str = _formater_cellule_hr(away_abbr, hr_away, home_abbr, hr_home)
        else:
            score_str = "—"
            total_runs = "—"
            hr_str = "—"

        pred = predictions_par_match.get((code_home, code_away))
        comparatif_str, resultat_icone = _comparer_prediction_vs_score(
            pred, nom_home, nom_away, home_score, away_score, a_commence
        )

        lignes.append({
            'Match': f"{nom_away} vs {nom_home}",
            'Statut': statut_str,
            'Score': score_str,
            'Total Runs': total_runs,
            'Home Runs': hr_str,
            'Comparatif Prédiction': comparatif_str,
            'Résultat vs Algo': resultat_icone,
        })

    return pd.DataFrame(lignes), None


# ------------------------------------------------------------------------------
# BILAN DES PRÉDICTIONS DE LA VEILLE (menu déroulant en tête de l'onglet "Résumé")
# ------------------------------------------------------------------------------
# npb.jp ne publie aucune ligne de paris officielle (contrairement aux sites de paris
# sportifs) : à défaut, la "ligne" Over/Under utilisée ci-dessous pour qualifier un
# match de "à forte marque" (Over) ou "à faible marque" (Under) est la moyenne réelle
# de runs cumulés (les deux équipes confondues) sur tous les matchs déjà joués cette
# saison - la référence la plus neutre et la plus objective disponible sans source de
# paris tierce.
@st.cache_data(show_spinner=False, ttl=3600)
def obtenir_ligne_over_under_saison(annee: int) -> float:
    """
    Moyenne de runs totaux (2 équipes cumulées) sur tous les matchs NPB déjà joués
    cette saison, tous mois confondus - sert de ligne de référence Over/Under pour le
    bilan des prédictions de la veille. Repli à 7.5 (ordre de grandeur usuel en NPB)
    si aucune donnée n'est encore disponible (tout début de saison).
    """
    totaux = []
    for mois in MOIS_SAISON:
        try:
            df_mois = charger_calendrier_mensuel(annee, mois)
        except Exception:
            continue
        if df_mois.empty:
            continue
        df_valides = df_mois.dropna(subset=['score_home', 'score_away'])
        if df_valides.empty:
            continue
        totaux.extend((df_valides['score_home'] + df_valides['score_away']).tolist())

    if not totaux:
        return 7.5
    return round(sum(totaux) / len(totaux), 2)


def _formater_vainqueur(nom_home: str, nom_away: str, home_score: int, away_score: int) -> str:
    """Nom de l'équipe gagnante, ou 'Match nul' (matchs NPB pouvant se terminer sur une égalité)."""
    if home_score == away_score:
        return "Match nul"
    return nom_home if home_score > away_score else nom_away


def _bilan_victoire(proba_home, proba_away, nom_home: str, nom_away: str, home_score: int, away_score: int):
    """Retourne (texte, icône) comparant l'équipe favorite annoncée hier à la gagnante réelle."""
    if proba_home is None or proba_away is None or pd.isna(proba_home) or pd.isna(proba_away):
        return "Prédiction non disponible", "⏳"
    if home_score == away_score:
        return "Match nul (pas de favori confirmé)", "⏳"
    favori = nom_home if proba_home >= proba_away else nom_away
    pct_favori = max(proba_home, proba_away)
    gagnant = nom_home if home_score > away_score else nom_away
    icone = "✅" if favori == gagnant else "❌"
    return f"{favori} favori à {pct_favori:.0f}% → vainqueur : {gagnant}", icone


def _bilan_over_under(total_runs_predit, total_runs_reel: int, ligne: float):
    """Retourne (texte, icône) comparant la projection Over/Under d'hier au total réel."""
    if total_runs_predit is None:
        return "Prédiction non disponible", "⏳"
    direction_predite = "Over" if total_runs_predit > ligne else "Under"
    direction_reelle = "Over" if total_runs_reel > ligne else "Under"
    icone = "✅" if direction_predite == direction_reelle else "❌"
    return (
        f"{direction_predite} annoncé (projection {total_runs_predit:.1f}, ligne {ligne:.1f}) "
        f"→ réel {total_runs_reel} ({direction_reelle})"
    ), icone


def _bilan_hr(candidats_home: list, candidats_away: list, hr_home_reels: list, hr_away_reels: list):
    """Retourne (texte, icône) : au moins un des joueurs surveillés hier a-t-il réellement frappé un HR ?"""
    candidats = [c for c in (candidats_home or []) + (candidats_away or []) if c]
    if not candidats:
        return "Prédiction non disponible", "⏳"
    scoreurs_reels = {nom for nom, _ in hr_home_reels} | {nom for nom, _ in hr_away_reels}
    touches = [c for c in candidats if c in scoreurs_reels]
    icone = "✅" if touches else "❌"
    texte = f"Surveillés : {', '.join(candidats)}"
    if touches:
        texte += f" → a frappé : {', '.join(touches)}"
    return texte, icone


@st.cache_data(show_spinner=False, ttl=3600)
def construire_bilan_veille(annee: int):
    """
    Construit le tableau "Résultats de la veille et Bilan des Prédictions" : reprend
    la structure du tableau des matchs du jour (`construire_resume_matchs_du_jour`),
    mais pour la date d'HIER (heure du Japon) et avec les matchs forcément terminés,
    enrichi de colonnes de bilan comparant la prédiction sauvegardée hier
    (`_sauvegarder_predictions_du_jour`, appelée automatiquement depuis
    `construire_donnees_hot_pronostics`) au résultat réel.

    Comme cette fonction n'est appelée QUE lorsque l'utilisateur ouvre le menu
    déroulant (cf. `afficher_bilan_predictions_veille`), elle n'a aucun coût au
    chargement initial de l'onglet "Résumé".

    Retourne (DataFrame, message_erreur), sur le même modèle que
    `construire_resume_matchs_du_jour` (jamais d'exception remontée à l'appelant).
    """
    hier_jst = datetime.now(TZ_JST) - timedelta(days=1)
    date_hier_str = hier_jst.strftime('%Y-%m-%d')

    if hier_jst.month not in MOIS_SAISON:
        return pd.DataFrame(), None  # hors saison (déc./janv./fév.) : pas de match hier

    try:
        df_mois = charger_calendrier_mensuel(hier_jst.year, hier_jst.month)
    except Exception as e:
        return pd.DataFrame(), (
            f"Impossible de récupérer les résultats d'hier pour le moment ({e}). "
            "Réessayez en rouvrant ce menu dans quelques instants."
        )

    if df_mois.empty:
        return pd.DataFrame(), None

    df_hier = df_mois[df_mois['Date'] == date_hier_str].dropna(subset=['score_home', 'score_away'])
    if df_hier.empty:
        return pd.DataFrame(), None

    predictions_hier = _charger_historique_predictions().get(date_hier_str, {}).get('matches', [])
    predictions_par_match = {(p.get('code_home'), p.get('code_away')): p for p in predictions_hier}

    ligne_ou = obtenir_ligne_over_under_saison(annee)

    lignes = []
    for _, g in df_hier.iterrows():
        code_home = (g.get('code_home') or '').lower()
        code_away = (g.get('code_away') or '').lower()
        if not code_home or not code_away:
            continue

        nom_home = TEAMS_NPB.get(code_home.upper(), g.get('nom_home') or '?')
        nom_away = TEAMS_NPB.get(code_away.upper(), g.get('nom_away') or '?')
        home_abbr, away_abbr = code_home.upper(), code_away.upper()

        try:
            home_score, away_score = int(g['score_home']), int(g['score_away'])
        except (TypeError, ValueError):
            continue
        total_reel = home_score + away_score

        hr_home = obtenir_hr_joueurs_match_jour(g.get('box_url'), True, date_hier_str, code_home, code_away)
        hr_away = obtenir_hr_joueurs_match_jour(g.get('box_url'), False, date_hier_str, code_home, code_away)

        pred = predictions_par_match.get((code_home, code_away))
        candidats_hr_home = pred.get('candidats_hr_home', []) if pred else []
        candidats_hr_away = pred.get('candidats_hr_away', []) if pred else []
        proba_home = pred.get('proba_home') if pred else None
        proba_away = pred.get('proba_away') if pred else None
        total_predit = pred.get('total_runs_predit') if pred else None

        texte_victoire, icone_victoire = _bilan_victoire(proba_home, proba_away, nom_home, nom_away, home_score, away_score)
        texte_ou, icone_ou = _bilan_over_under(total_predit, total_reel, ligne_ou)
        texte_hr, icone_hr = _bilan_hr(candidats_hr_home, candidats_hr_away, hr_home, hr_away)

        lignes.append({
            'Match': f"{nom_away} vs {nom_home}",
            'Statut': "Terminé",
            'Score': f"{away_abbr} {away_score} - {home_abbr} {home_score}",
            'Total Runs': str(total_reel),
            'Home Runs': _formater_cellule_hr(away_abbr, hr_away, home_abbr, hr_home),
            'Vainqueur': _formater_vainqueur(nom_home, nom_away, home_score, away_score),
            'Victoire prédite': texte_victoire,
            'Over/Under prédit': texte_ou,
            'HR surveillés': texte_hr,
            'Bilan': f"Victoire {icone_victoire} · Over/Under {icone_ou} · HR {icone_hr}",
        })

    return pd.DataFrame(lignes), None


def afficher_bilan_predictions_veille(annee: int):
    """
    Corps du menu déroulant "📅 Résultats de la veille et Bilan des Prédictions" :
    appelé uniquement quand ce menu est ouvert (cf. garde `expander.open` dans
    `afficher_onglet_resume`), donc sans coût réseau tant que l'utilisateur ne l'a
    pas déplié.
    """
    if annee != ANNEE_COURANTE:
        st.info(
            f"Le bilan de la veille n'est disponible que pour la saison en cours "
            f"({ANNEE_COURANTE})."
        )
        return

    with st.spinner("Récupération des résultats d'hier et calcul du bilan des prédictions..."):
        df_bilan, message_erreur = construire_bilan_veille(annee)

    if message_erreur:
        st.error(f"⚠️ {message_erreur}")
        return

    if df_bilan.empty:
        st.info(
            "Aucun match NPB terminé hier (heure du Japon), ou aucune prédiction n'avait été "
            "sauvegardée hier pour ces matchs (l'onglet Résumé ou Hot Pronostics doit avoir été "
            "consulté au moins une fois dans la journée pour qu'un instantané soit archivé)."
        )
        return

    st.dataframe(
        df_bilan,
        column_config={
            "Match": st.column_config.TextColumn("Match", width="medium"),
            "Statut": st.column_config.TextColumn("Statut", width="small"),
            "Score": st.column_config.TextColumn("Score", width="small"),
            "Total Runs": st.column_config.TextColumn("Total Runs", width="small"),
            "Home Runs": st.column_config.TextColumn("Home Runs", width="large"),
            "Vainqueur": st.column_config.TextColumn("Vainqueur", width="medium"),
            "Victoire prédite": st.column_config.TextColumn("Victoire prédite", width="large"),
            "Over/Under prédit": st.column_config.TextColumn("Over/Under prédit", width="large"),
            "HR surveillés": st.column_config.TextColumn("HR surveillés", width="large"),
            "Bilan": st.column_config.TextColumn("Bilan", width="large"),
        },
        hide_index=True,
    )

    st.caption(
        "**Méthodologie** — Victoire : ✅ si l'équipe favorite (probabilité la plus haute) a "
        "réellement gagné. Over/Under : ligne de référence = moyenne réelle de runs cumulés par "
        "match sur la saison en cours ; ✅ si notre projection (moyenne de runs des 10 derniers "
        "matchs des deux équipes) était du même côté de cette ligne que le résultat réel. HR : ✅ "
        "si au moins un des joueurs les plus en forme au HR (10 derniers matchs) de chaque équipe "
        "a effectivement frappé un home run dans ce match. ⏳ = aucune prédiction n'avait été "
        "archivée pour ce match (application non consultée la veille) ou match nul. Les "
        "prédictions ne sont archivées qu'au moment où l'onglet Résumé ou Hot Pronostics est "
        "consulté ce jour-là (pas de calcul en tâche de fond)."
    )


@st.fragment
def afficher_onglet_resume(annee: int):
    """
    Corps de l'onglet "Résumé" (menu déroulant "Bilan de la veille" + bouton de
    rafraîchissement + tableau du jour), encapsulé dans un `st.fragment` : cliquer sur
    le bouton, ou ouvrir/fermer le menu déroulant, ne relance QUE cette fonction, sans
    recharger le reste de l'application (sidebar, autres onglets) ni la page web
    entière.
    """
    # --- Menu déroulant "Bilan des Prédictions" de la veille, tout en haut de
    # l'onglet, au-dessus du tableau des matchs du jour. `on_change="rerun"` rend la
    # propriété `.open` dynamique (True/False selon l'état du menu) : le contenu
    # (requête réseau vers npb.jp incluse) n'est donc calculé QUE si l'utilisateur a
    # effectivement déplié le menu, jamais au chargement initial de l'onglet.
    expander_veille = st.expander(
        "📅 Résultats de la veille et Bilan des Prédictions", on_change="rerun"
    )
    if expander_veille.open:
        with expander_veille:
            afficher_bilan_predictions_veille(annee)

    st.markdown("---")

    if 'resume_cache_bust' not in st.session_state:
        st.session_state.resume_cache_bust = 0
    if 'resume_derniere_actualisation' not in st.session_state:
        st.session_state.resume_derniere_actualisation = None

    col_bouton, col_info = st.columns([1, 2])
    with col_bouton:
        if st.button("🔄 Rafraîchir les scores en direct"):
            st.session_state.resume_cache_bust += 1
            st.session_state.resume_derniere_actualisation = datetime.now(TZ_PARIS)

    with col_info:
        if st.session_state.resume_derniere_actualisation:
            st.caption(
                "Dernière actualisation manuelle : "
                f"{st.session_state.resume_derniere_actualisation.strftime('%H:%M:%S')} (heure française)."
            )
        else:
            st.caption("Cliquez sur le bouton pour actualiser les scores en direct.")

    if annee != ANNEE_COURANTE:
        st.info(
            f"Le résumé du jour n'est disponible que pour la saison en cours "
            f"({ANNEE_COURANTE}). Sélectionnez {ANNEE_COURANTE} dans le menu de gauche."
        )
        return

    with st.spinner("Récupération des scores en direct..."):
        df_resume, message_erreur = construire_resume_matchs_du_jour(
            annee, st.session_state.resume_cache_bust
        )

    if message_erreur:
        st.error(f"⚠️ {message_erreur}")

    if df_resume.empty:
        if message_erreur is None:
            st.info("Aucun match n'est prévu aujourd'hui (heure du Japon).")
        return

    st.dataframe(
        df_resume,
        column_config={
            "Match": st.column_config.TextColumn("Match", width="medium"),
            "Statut": st.column_config.TextColumn("Statut", width="small"),
            "Score": st.column_config.TextColumn("Score", width="small"),
            "Total Runs": st.column_config.TextColumn("Total Runs", width="small"),
            "Home Runs": st.column_config.TextColumn("Home Runs", width="large"),
            "Comparatif Prédiction": st.column_config.TextColumn("Comparatif Prédiction", width="medium"),
            "Résultat vs Algo": st.column_config.TextColumn("Résultat vs Algo", width="small"),
        },
        hide_index=True,
    )

    st.caption(
        "✅ = l'équipe favorite de notre algorithme mène ou a gagné · ❌ = elle est menée ou a "
        "perdu · ⏳ = match pas encore commencé, à égalité, ou prédiction pas encore disponible. "
        "Le score, le total de runs et les home runs ne sont affichés qu'une fois le match "
        "commencé. Contrairement à MLB, npb.jp n'indique pas la manche en cours : le statut "
        "\"En cours\" ne précise donc pas de détail supplémentaire."
    )


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
    "📊 Résumé",
    "🔥 Hot Pronostics",
    "📊 Analyse par Équipe",
    "🔮 Prédictions du jour"
], on_change="rerun")

# --------------------------------------------------------------
# ONGLET 0: RÉSUMÉ (scores en direct et terminés du jour, heure du Japon)
# --------------------------------------------------------------
with onglets[0]:
    if onglets[0].open:
        st.header("📊 Résumé du jour")
        st.markdown("### Suivi des confrontations NPB du jour (heure du Japon)")
        afficher_onglet_resume(annee)

# --------------------------------------------------------------
# ONGLET 1: HOT PRONOSTICS (scan global de tous les matchs du jour, heure du Japon)
# --------------------------------------------------------------
with onglets[1]:
    if onglets[1].open:
        st.header("🔥 Hot Pronostics du jour")
        st.markdown("### Les meilleurs pronostics du jour, tous matchs confondus (heure du Japon)")
        st.caption(
            "⚠️ Estimations statistiques automatiques calculées à partir des lanceurs partants "
            "annoncés (annoncés la veille au Japon) et de la forme récente des joueurs (10 "
            "derniers matchs). Ce ne sont pas des garanties de résultat : simples heuristiques, "
            "à utiliser uniquement à titre informatif, avec discernement si vous vous en servez "
            "pour parier."
        )

        if annee != ANNEE_COURANTE:
            st.info(
                f"Les Hot Pronostics ne sont disponibles que pour la saison en cours "
                f"({ANNEE_COURANTE}). Sélectionnez {ANNEE_COURANTE} dans le menu de gauche."
            )
        else:
            with st.spinner("Analyse de tous les matchs du jour (lanceurs annoncés, forme récente des 10 derniers matchs)..."):
                matchs_jour, df_top5_hr, df_top5_runs, df_victoires = construire_donnees_hot_pronostics(annee)

            if not matchs_jour:
                st.info("Aucun match n'est prévu aujourd'hui (heure du Japon).")
            else:
                nb_lanceurs_annonces = sum(
                    1 for m in matchs_jour if m['home_pitcher'] or m['away_pitcher']
                )
                st.caption(
                    f"📅 {len(matchs_jour)} match(s) au programme aujourd'hui (heure du Japon) · "
                    f"lanceur(s) partant(s) annoncé(s) pour {nb_lanceurs_annonces} match(s) sur "
                    f"{len(matchs_jour)} (au Japon, les partants sont annoncés la veille du match - "
                    "revenez plus tard pour voir apparaître les matchs restants)."
                )

                st.markdown("---")
                st.subheader("💣 Top 5 Home Runs probables")
                if df_top5_hr.empty:
                    st.info(
                        "Pas assez de données récentes (HR sur les 10 derniers matchs) pour établir "
                        "un classement pour le moment."
                    )
                else:
                    st.dataframe(
                        df_top5_hr,
                        column_config={
                            "HR (10 derniers matchs)": st.column_config.NumberColumn("HR (10 derniers matchs)", format="%d"),
                            "HR/9 lanceur adverse": st.column_config.NumberColumn("HR/9 lanceur adverse", format="%.2f"),
                            "Indice HR (/100)": st.column_config.ProgressColumn(
                                "Indice HR (/100)", min_value=0, max_value=100, format="%.0f"
                            ),
                        },
                        hide_index=True,
                    )

                st.markdown("---")
                st.subheader("🏃 Top 5 joueurs pour marquer un run")
                if df_top5_runs.empty:
                    st.info(
                        "Pas assez de données récentes (runs sur les 10 derniers matchs) pour établir "
                        "un classement pour le moment."
                    )
                else:
                    st.dataframe(
                        df_top5_runs,
                        column_config={
                            "Runs (10 derniers matchs)": st.column_config.NumberColumn("Runs (10 derniers matchs)", format="%d"),
                            "ERA lanceur adverse": st.column_config.NumberColumn("ERA lanceur adverse", format="%.2f"),
                            "Indice Run (/100)": st.column_config.ProgressColumn(
                                "Indice Run (/100)", min_value=0, max_value=100, format="%.0f"
                            ),
                        },
                        hide_index=True,
                    )

                st.markdown("---")
                st.subheader("🎲 Probabilités Win/Lose du jour")
                if df_victoires.empty:
                    st.info("Aucune donnée de probabilité de victoire disponible pour le moment.")
                else:
                    st.dataframe(
                        df_victoires,
                        column_config={
                            "Proba Domicile (%)": st.column_config.ProgressColumn(
                                "Proba Domicile (%)", min_value=0, max_value=100, format="%.1f%%"
                            ),
                            "Proba Extérieur (%)": st.column_config.ProgressColumn(
                                "Proba Extérieur (%)", min_value=0, max_value=100, format="%.1f%%"
                            ),
                        },
                        hide_index=True,
                    )

                st.caption(
                    "**Méthodologie** — Home Runs : HR sur les 10 derniers matchs (60%) + HR/9 du "
                    "lanceur partant adverse annoncé (40%). Runs : runs sur les 10 derniers matchs "
                    "(60%) + ERA du lanceur partant adverse annoncé (40%). Win/Lose : moyenne de "
                    "runs marqués sur les 10 derniers matchs de chaque équipe + ERA/WHIP des "
                    "lanceurs partants annoncés du jour (même modèle que l'onglet \"Prédictions du "
                    "jour\", détaillé plus bas). npb.jp ne publiant pas de lineup officielle à "
                    "l'avance (contrairement à MLB StatsAPI), les candidats HR/Runs sont les "
                    "joueurs les plus en forme de chaque équipe (10 derniers matchs) plutôt que les "
                    "titulaires confirmés du jour. Chaque indice est normalisé sur l'ensemble des "
                    "candidats du jour, donc relatif à la journée en cours."
                )

# --------------------------------------------------------------
# ONGLET 2: ANALYSE PAR ÉQUIPE
# --------------------------------------------------------------
with onglets[2]:
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
# ONGLET 3: PRÉDICTIONS DU JOUR
# --------------------------------------------------------------
with onglets[3]:
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

            # Les stats du lanceur adverse ET celles de NOTRE lanceur (saison en cours) ont
            # déjà été récupérées par `obtenir_match_du_jour`, de manière SYMÉTRIQUE (même
            # fonction `obtenir_infos_lanceur` appelée pour les deux lanceurs annoncés), il
            # n'y a donc plus besoin d'appel réseau séparé ici pour aucun des deux camps.
            stats_lanceur_nous = match_du_jour.get('stats_lanceur_nous')
            stats_lanceur_adverse = match_du_jour['stats_lanceur_adverse']

            if stats_lanceur_adverse and stats_lanceur_adverse.get('era'):
                st.caption(
                    f"Stats saison {annee} de {stats_lanceur_adverse['nom']} : "
                    f"ERA {stats_lanceur_adverse['era']:.2f} · WHIP {stats_lanceur_adverse['whip']:.2f} · "
                    f"{stats_lanceur_adverse['hr_alloues']} HR alloués · "
                    f"{stats_lanceur_adverse['matchs_titulaire']} apparitions"
                )
            elif match_du_jour['lanceur_adverse']:
                st.caption("Statistiques du lanceur adverse indisponibles pour le moment.")

            # Moyenne de runs CONCÉDÉS par notre équipe sur ses 10 derniers matchs : calculée
            # UNE SEULE FOIS ici, puis réutilisée à la fois par le module "Probabilité de
            # Victoire" ci-dessous (comme proxy de l'attaque adverse, voir docstring de
            # `predire_probabilite_victoire`) et par le module de prédiction des Runs plus
            # bas (qui l'utilisait déjà comme proxy identique).
            moyenne_ra_10 = pd.to_numeric(
                df_matchs.tail(10).get('RA', pd.Series(dtype=float)), errors='coerce'
            ).mean()

            # --------------------------------------------------------------
            # MODULE : PROBABILITÉ DE VICTOIRE
            # --------------------------------------------------------------
            st.markdown("---")
            st.subheader("🎲 Probabilité de Victoire")

            pct_nous, pct_adverse = predire_probabilite_victoire(
                moyenne_runs_10,
                moyenne_ra_10,
                stats_lanceur_nous,
                stats_lanceur_adverse,
                match_du_jour['est_domicile'],
            )

            col_proba1, col_proba2 = st.columns(2)
            with col_proba1:
                st.metric(f"{EQUIPES_NPB.get(equipe_abbr, equipe_abbr)}", f"{pct_nous:.0f}%")
            with col_proba2:
                st.metric(f"{match_du_jour['adversaire']}", f"{pct_adverse:.0f}%")
            st.progress(pct_nous / 100)

            st.caption(
                "Estimation basée sur (1) l'ERA/WHIP des lanceurs partants annoncés des deux "
                "équipes (facteur principal), (2) la moyenne de runs marqués/concédés sur les "
                "10 derniers matchs (dynamique offensive récente), et (3) un léger bonus de "
                "+3 points de pourcentage pour l'équipe qui joue à domicile (~53-54% de "
                "victoires à domicile en moyenne dans le baseball professionnel). "
                "⚠️ Simple heuristique, PAS un modèle statistique validé : ne reflète pas "
                "tous les facteurs d'un vrai match (composition exacte de l'équipe, bullpen, "
                "météo, blessures de dernière minute, etc.)."
            )

            lanceur_nous_ok = bool(stats_lanceur_nous and stats_lanceur_nous.get('era'))
            lanceur_adv_ok = bool(stats_lanceur_adverse and stats_lanceur_adverse.get('era'))
            if not (lanceur_nous_ok and lanceur_adv_ok):
                st.info(
                    "ℹ️ Stats ERA/WHIP indisponibles pour au moins un des deux lanceurs "
                    "annoncés (facteur neutralisé pour le(s) lanceur(s) concerné(s)) : "
                    "l'estimation ci-dessus est donc moins fiable que d'habitude."
                )

            st.markdown("---")
            st.subheader("📊 Module de prédiction des Runs")

            if moyenne_runs_10 is None:
                st.info("Pas assez de données récentes pour estimer les runs de cette équipe.")
            else:
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
                        if stats_lanceur_adverse and stats_lanceur_adverse.get('era')
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
