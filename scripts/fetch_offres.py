#!/usr/bin/env python3
"""Veille multi-sources des offres de préparateur·trice en pharmacie -> offres.json.

Sources actives (chacune indépendante) :
  - Jooble : JOOBLE_API_KEY (API REST, POST https://fr.jooble.org/api/{clé})
  - Choisir le service public (CSP) : données ouvertes data.gouv.fr, sans clé (cache hebdomadaire data/csp.json)
Désactivée mais conservée : France Travail (ENABLE_FRANCE_TRAVAIL = False ; FT_CLIENT_ID, FT_CLIENT_SECRET).

Les identifiants viennent des variables d'environnement (chargées depuis .env si présent).
Aucun secret n'est jamais affiché : les exceptions réseau sont réduites à leur type.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv est optionnel
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "offres.json"
CSP_CACHE = ROOT / "data" / "csp.json"

TIMEOUT = 30
MAX_RETRIES_429 = 5
DEPARTEMENTS = ["75", "92", "93", "94"]
ZONES = ["Paris", "Hauts-de-Seine", "Seine-Saint-Denis", "Val-de-Marne"]
SEARCH_TERMS = "préparateur pharmacie"
MAX_PAGES = 5
MAX_AGE_DAYS = 45  # offres plus anciennes exclues
PLACEHOLDER = "Non communiqué"
TO_CHECK = "À vérifier"

# France Travail : code conservé mais source désactivée (l'application n'a pas accès à l'API : invalid_scope).
ENABLE_FRANCE_TRAVAIL = False

# Choisir le service public : code conservé mais source désactivée (peu d'offres pertinentes, CSV en retard sur le site).
ENABLE_CSP = False

# Ordre de priorité en cas de doublon entre sources (le premier l'emporte)
SOURCE_PRIORITY = ["csp", "ft", "jb"]  # CSP > (France Travail) > Jooble
SOURCE_LABELS = {
    "ft": "France Travail",
    "jb": "Jooble",
    "csp": "Choisir le service public",
}

HIGH_TERMS = [
    "PUI", "pharmacie à usage intérieur", "hôpital", "hôpitaux", "hospitalier",
    "clinique", "stérilisation", "HAD", "EHPAD", "chimiothérapie", "dispositifs médicaux",
]


def fold(text: str) -> str:
    """Minuscules, sans accents, espaces normalisés."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def fold_words(text: str) -> str:
    """fold + tirets/apostrophes/ponctuation remplacés par des espaces (comparaison de noms de lieux)."""
    return re.sub(r"[^a-z0-9]+", " ", fold(text)).strip()


HIGH_RE = re.compile(r"\b(?:" + "|".join(re.escape(fold(t)) for t in HIGH_TERMS) + r")\b")
DEPPH_RE = re.compile(r"\bdepph\b")
EXCLUDE_RE = re.compile(r"apprenti|alternan|\bstages?\b|stagiaire")


class SourceError(Exception):
    pass


class SourceSkipped(Exception):
    """Identifiants absents : la source est ignorée."""


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def http_error(prefix: str, exc: Exception) -> SourceError:
    # Ne jamais citer str(exc) : une exception requests contient l'URL (donc parfois une clé).
    return SourceError(f"{prefix} : erreur réseau ({type(exc).__name__}).")


# --------------------------------------------------------------------------- #
# Format commun
# --------------------------------------------------------------------------- #

def make_offer(*, source_id, title, employer, location, contract, day, pay, link, description,
               extra_text="", note=None) -> dict:
    """Offre au format de index.html, adéquation et DEPPH détectés comme pour France Travail."""
    text = fold(f"{title} {description} {extra_text}")
    offer = {
        "sourceId": source_id,
        "status": "Nouvelle",
        "title": title or "Sans intitulé",
        "employer": employer or PLACEHOLDER,
        "location": location or PLACEHOLDER,
        "contract": contract or TO_CHECK,
        "date": (day or "")[:10],
        "pay": pay or PLACEHOLDER,
        "adequation": "Élevée" if HIGH_RE.search(text) else "Moyenne",
        "diploma": "",
        "note": re.sub(r"\s+", " ", note if note is not None else description or "").strip()[:150],
        "link": link or "",
    }
    if DEPPH_RE.search(text):
        offer["diploma"] = "DEPPH mentionné — à vérifier"
        offer["status"] = TO_CHECK
    return offer


# --------------------------------------------------------------------------- #
# Filtres communs
# --------------------------------------------------------------------------- #

def title_ok(title: str) -> bool:
    """Intitulé contenant « préparat » et « pharma », hors apprenti/alternance/stage."""
    f = fold(title)
    return "preparat" in f and "pharma" in f and not EXCLUDE_RE.search(f)


# Départements hors zone (noms), pour écarter un lieu explicitement ailleurs.
OTHER_DEPT_NAMES = [
    "Ain", "Aisne", "Allier", "Alpes-de-Haute-Provence", "Hautes-Alpes", "Alpes-Maritimes", "Ardèche",
    "Ardennes", "Ariège", "Aube", "Aude", "Aveyron", "Bouches-du-Rhône", "Calvados", "Cantal",
    "Charente", "Charente-Maritime", "Cher", "Corrèze", "Corse", "Côte-d'Or", "Côtes-d'Armor", "Creuse",
    "Dordogne", "Doubs", "Drôme", "Eure", "Eure-et-Loir", "Finistère", "Gard", "Haute-Garonne", "Gers",
    "Gironde", "Hérault", "Ille-et-Vilaine", "Indre", "Indre-et-Loire", "Isère", "Jura", "Landes",
    "Loir-et-Cher", "Loire", "Haute-Loire", "Loire-Atlantique", "Loiret", "Lot", "Lot-et-Garonne",
    "Lozère", "Maine-et-Loire", "Manche", "Marne", "Haute-Marne", "Mayenne", "Meurthe-et-Moselle",
    "Meuse", "Morbihan", "Moselle", "Nièvre", "Nord", "Oise", "Orne", "Pas-de-Calais", "Puy-de-Dôme",
    "Pyrénées-Atlantiques", "Hautes-Pyrénées", "Pyrénées-Orientales", "Bas-Rhin", "Haut-Rhin", "Rhône",
    "Haute-Saône", "Saône-et-Loire", "Sarthe", "Savoie", "Haute-Savoie", "Seine-Maritime",
    "Seine-et-Marne", "Yvelines", "Deux-Sèvres", "Somme", "Tarn", "Tarn-et-Garonne", "Var", "Vaucluse",
    "Vendée", "Vienne", "Haute-Vienne", "Vosges", "Yonne", "Territoire de Belfort", "Essonne",
    "Val-d'Oise", "Guadeloupe", "Martinique", "Guyane", "La Réunion", "Mayotte",
]
OTHER_DEPT_RE = re.compile(r"\b(?:" + "|".join(sorted((re.escape(fold_words(n)) for n in OTHER_DEPT_NAMES),
                                                      key=len, reverse=True)) + r")\b")
ZONE_NAME_RE = re.compile(r"\b(?:paris|hauts de seine|seine saint denis|val de marne)\b")
POSTAL_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
PAREN_DEPT_RE = re.compile(r"\(\s*(\d{2,3}|2[AB])\s*\)")
LEAD_DEPT_RE = re.compile(r"^\s*(\d{2,3})\s*-")


def classify_location(text: str) -> str:
    """'in' (zone 75/92/93/94), 'out' (explicitement ailleurs) ou 'ambiguous'."""
    postals = [p[:2] for p in POSTAL_RE.findall(text)]
    depts = PAREN_DEPT_RE.findall(text)
    m = LEAD_DEPT_RE.match(text)
    if m:
        depts.append(m.group(1))
    words = fold_words(text)
    if any(p in DEPARTEMENTS for p in postals) or any(d in DEPARTEMENTS for d in depts) \
            or ZONE_NAME_RE.search(words):
        return "in"
    if postals or depts or OTHER_DEPT_RE.search(words):
        return "out"
    return "ambiguous"


def apply_filters(offers: list[tuple]) -> tuple[list[dict], dict]:
    """offers : liste de (offre, texte de localisation complet). Renvoie (offres retenues, stats)."""
    kept: list[dict] = []
    stats = {"title": 0, "location": 0, "age": 0}
    for offer, loc_text in offers:
        if not title_ok(offer["title"]):
            stats["title"] += 1
            continue
        if offer["date"] and offer["date"] < (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat():
            stats["age"] += 1
            continue
        verdict = classify_location(loc_text)
        if verdict == "out":
            stats["location"] += 1
            continue
        if verdict == "ambiguous" and offer["status"] == "Nouvelle":
            offer["status"] = TO_CHECK
        kept.append(offer)
    return kept, stats


# --------------------------------------------------------------------------- #
# Source : France Travail (code existant conservé)
# --------------------------------------------------------------------------- #

FT_TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
FT_SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
FT_SCOPE = "api_offresdemploiv2 o2dsoffre"
FT_ROME_CODE = "J1307"
FT_PAGE_SIZE = 150
FT_MAX_OFFSET = 1149  # limite de l'API : range ne dépasse pas 1149


def ft_get_token() -> str:
    client_id = os.environ.get("FT_CLIENT_ID")
    client_secret = os.environ.get("FT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SourceSkipped("FT_CLIENT_ID et/ou FT_CLIENT_SECRET manquants")
    try:
        r = requests.post(
            FT_TOKEN_URL,
            params={"realm": "/partenaire"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": FT_SCOPE,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise http_error("Authentification France Travail", e) from e
    if r.status_code != 200:
        try:
            code = r.json().get("error")  # ex. invalid_scope, invalid_client : code OAuth, jamais un secret
        except ValueError:
            code = None
        hint = " — l'application n'est pas abonnée à l'API Offres d'emploi v2 (francetravail.io)" if code == "invalid_scope" \
            else " — vérifiez FT_CLIENT_ID / FT_CLIENT_SECRET"
        raise SourceError(f"Authentification refusée (HTTP {r.status_code}, {code or 'erreur inconnue'}){hint}.")
    token = r.json().get("access_token")
    if not token:
        raise SourceError("Réponse d'authentification sans access_token.")
    return token


def ft_request_page(token: str, departement: str, start: int, end: int):
    params = {"codeROME": FT_ROME_CODE, "departement": departement, "range": f"{start}-{end}"}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    for attempt in range(1, MAX_RETRIES_429 + 1):
        try:
            r = requests.get(FT_SEARCH_URL, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise http_error(f"France Travail, département {departement}", e) from e
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2.0 * attempt
            print(f"  429 (trop de requêtes) — attente {wait:.0f}s, essai {attempt}/{MAX_RETRIES_429}", file=sys.stderr)
            time.sleep(wait)
            continue
        return r
    raise SourceError(f"France Travail, département {departement} : trop de réponses 429, abandon.")


def ft_search_departement(token: str, departement: str) -> list[dict]:
    results: list[dict] = []
    start = 0
    while start <= FT_MAX_OFFSET:
        end = min(start + FT_PAGE_SIZE - 1, FT_MAX_OFFSET)
        r = ft_request_page(token, departement, start, end)
        if r.status_code == 204:  # aucun résultat : pas de corps JSON
            break
        if r.status_code not in (200, 206):
            raise SourceError(f"France Travail, département {departement} : HTTP {r.status_code}")
        results.extend(r.json().get("resultats", []))
        if r.status_code == 200:  # dernière page
            break
        start = end + 1  # 206 : page suivante
    return results


def ft_contract(raw: dict) -> str:
    code = (raw.get("typeContrat") or "").upper()
    if code == "CDI":
        return "CDI"
    if code == "CDD":
        return "CDD"
    if code == "MIS":
        return "Intérim"
    return raw.get("typeContratLibelle") or code or TO_CHECK


def ft_convert(raw: dict) -> tuple[dict, str]:
    oid = str(raw["id"])
    lieu = raw.get("lieuTravail") or {}
    offer = make_offer(
        source_id=f"ft-{oid}",
        title=raw.get("intitule"),
        employer=(raw.get("entreprise") or {}).get("nom"),
        location=lieu.get("libelle"),
        contract=ft_contract(raw),
        day=raw.get("dateCreation"),
        pay=(raw.get("salaire") or {}).get("libelle"),
        link=(raw.get("origineOffre") or {}).get("urlOrigine")
        or f"https://candidat.francetravail.fr/offres/recherche/detail/{oid}",
        description=raw.get("description") or "",
    )
    return offer, f"{lieu.get('libelle') or ''} {lieu.get('codePostal') or ''}"


def fetch_france_travail() -> tuple[list[tuple[dict, str]], int]:
    token = ft_get_token()
    print(f"  Jeton obtenu (longueur : {len(token)}).")
    by_id: dict[str, dict] = {}
    for dep in DEPARTEMENTS:
        raws = ft_search_departement(token, dep)
        print(f"  Département {dep} : {len(raws)} offre(s)")
        for raw in raws:
            by_id.setdefault(str(raw["id"]), raw)
    return [ft_convert(r) for r in by_id.values()], len(by_id)


# --------------------------------------------------------------------------- #
# Source : Jooble  (POST https://fr.jooble.org/api/{clé})
# --------------------------------------------------------------------------- #

JB_PAGE_SIZE = 50
JB_URL = "https://fr.jooble.org/api/{key}"  # clé liée au domaine régional : jooble.org répond 403


def jb_contract(raw: dict, snippet: str = "") -> str:
    """Contrat déduit de l'intitulé, de l'extrait et du type « Intérimaire » (sans casse ni accents)."""
    text = fold(f"{raw.get('title') or ''} {snippet}")
    if re.search(r"\bcdi\b|duree indeterminee", text):
        return "CDI"
    if re.search(r"\bcdd\b|duree determinee", text):
        return "CDD"
    if re.search(r"interim|\bmission\b", f"{text} {fold(raw.get('type') or '')}"):
        return "Intérim"
    return TO_CHECK


def jb_convert(raw: dict) -> tuple[dict, str]:
    link = raw.get("link") or ""
    jid = raw.get("id")
    sid = f"jb-{jid}" if jid not in (None, "", 0) else "jb-" + hashlib.sha1(link.encode()).hexdigest()[:12]
    loc = raw.get("location") or ""
    snippet = strip_html(raw.get("snippet"))
    kind = (raw.get("type") or "").strip()  # Temps plein / partiel : dans la note, pas dans le contrat
    offer = make_offer(
        source_id=sid,
        title=strip_html(raw.get("title")),
        employer=raw.get("company"),
        location=loc,
        contract=jb_contract(raw, snippet),
        day=raw.get("updated"),
        pay=(raw.get("salary") or "").strip(),
        link=link,
        description=snippet,
        note=f"{snippet[:110]} · {kind}" if snippet and kind else (snippet[:110] or kind),
    )
    return offer, loc


def fetch_jooble() -> tuple[list[tuple[dict, str]], int]:
    key = os.environ.get("JOOBLE_API_KEY")
    if not key:
        raise SourceSkipped("JOOBLE_API_KEY manquant")
    url = JB_URL.format(key=key)
    by_key: dict[str, dict] = {}
    for zone in ZONES:
        n_zone = 0
        for page in range(1, MAX_PAGES + 1):
            body = {"keywords": SEARCH_TERMS, "location": zone, "page": page, "ResultOnPage": JB_PAGE_SIZE}
            try:
                r = requests.post(url, json=body, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise http_error(f"Jooble ({zone})", e) from e
            if r.status_code != 200:
                raise SourceError(f"Jooble ({zone}, page {page}) : HTTP {r.status_code}"
                                  + (" — vérifiez JOOBLE_API_KEY" if r.status_code in (401, 403, 404) else ""))
            try:
                data = r.json()
            except ValueError as e:
                raise SourceError(f"Jooble ({zone}) : réponse non JSON.") from e
            jobs = data.get("jobs") or []
            if not jobs:
                break
            for raw in jobs:
                by_key.setdefault(str(raw.get("id") or raw.get("link")), raw)
            n_zone += len(jobs)
            total = data.get("totalCount")
            if isinstance(total, int) and n_zone >= total:
                break
        print(f"  {zone} : {n_zone} résultat(s)")
    return [jb_convert(r) for r in by_key.values()], len(by_key)


# --------------------------------------------------------------------------- #
# Source : Choisir le service public (open data, CSV ~120 Mo, cache hebdomadaire)
# --------------------------------------------------------------------------- #

CSP_DATASET_API = "https://www.data.gouv.fr/api/1/datasets/les-offres-diffusees-sur-choisir-le-service-public/"
# Pas de colonne URL dans le CSV : https://choisirleservicepublic.gouv.fr/offre-emploi/<Référence>/ ouvre l'offre
# (vérifié pour les références numériques, APHP_… et DEF_… ; une offre dépubliée redirige vers /offre-depubliee/).
CSP_OFFER_URL = "https://choisirleservicepublic.gouv.fr/offre-emploi/{ref}/"
CSP_UNPUBLISHED_MARK = "offre-depubliee"
CSP_CACHE_DAYS = 7
CSP_SCHEMA = 3  # à incrémenter si la conversion change : invalide le cache
CSP_NO_END = 9999  # « 31/12/9999 » = pas de date de fin
CSP_META: dict = {"versants": {}, "latest_publication": "", "unpublished": 0}


def csp_pick_resource() -> dict:
    try:
        r = requests.get(CSP_DATASET_API, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise http_error("data.gouv.fr", e) from e
    if r.status_code != 200:
        raise SourceError(f"data.gouv.fr : HTTP {r.status_code}")
    resources = [
        res for res in r.json().get("resources", [])
        if (res.get("format") or "").lower() == "csv" and res.get("url")
    ]
    if not resources:
        raise SourceError("data.gouv.fr : aucun CSV dans le jeu de données.")
    # CSV courant = ressource la plus récente par last_modified (le nom du fichier ne fait pas foi)
    return max(resources, key=lambda res: res.get("last_modified") or "")


def parse_fr_date(value: str) -> date | None:
    m = re.fullmatch(r"\s*(\d{2})/(\d{2})/(\d{4})\s*", value or "")
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y >= CSP_NO_END:
        return date.max
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def csp_contract(row: dict) -> str:
    emploi = fold(row.get("Nature de l'emploi") or "")
    duree = (row.get("Durée du contrat") or "").strip()
    if "titulaire" in emploi:
        return "CDI / titulaire"
    if "contractuel" in emploi and duree:
        return f"CDD ({duree})"
    return (row.get("Nature de contrat") or "").strip() or TO_CHECK


def csp_is_hospital(row: dict) -> bool:
    return "hospitalier" in fold(row.get("Versant") or "") or (row.get("Référence") or "").startswith("APHP_")


def csp_convert(row: dict) -> tuple[dict, str, date | None]:
    ref = (row.get("Référence") or "").strip()
    place = (row.get("Lieu d'affectation") or "").strip()
    zone = (row.get("Localisation du poste") or "").strip()
    # Lieu d'affectation sinon Localisation ; une adresse complète est réduite à « code ville »
    m = re.search(r"(\d{5})\s+([^\d,;]+?)\s*(?:cedex.*)?$", place, re.I)
    location = (f"{m.group(1)} {m.group(2).strip().title()}" if m else place) or zone
    employeur = (row.get("Employeur") or "").strip()
    # « Employeur » contient parfois un paragraphe de présentation : on retombe alors sur l'organisme
    employer = employeur if 0 < len(employeur) <= 120 else (row.get("Organisme de rattachement") or "").strip()
    day = parse_fr_date(row.get("Date de première publication") or "") \
        or parse_fr_date(row.get("Date de début de publication par défaut") or "")
    versant = (row.get("Versant") or "").strip() or ("Fonction Publique Hospitalière (AP-HP)" if ref.startswith("APHP_") else "")
    note = " · ".join(x for x in [versant, (row.get("Catégorie") or "").strip(), f"Réf. {ref}"] if x)
    offer = make_offer(
        source_id=f"csp-{ref}",
        title=(row.get("Intitulé du poste") or "").strip(),
        employer=employer,
        location=location,
        contract=csp_contract(row),
        day=day.isoformat() if day and day != date.max else "",
        pay=PLACEHOLDER,
        link=CSP_OFFER_URL.format(ref=ref),
        description=(row.get("Compétences attendues") or ""),
        extra_text=f"{employeur} {row.get('Organisme de rattachement') or ''}",
        note=note,
    )
    if csp_is_hospital(row):
        offer["adequation"] = "Élevée"
    end = parse_fr_date(row.get("Date de fin de publication par défaut") or "")
    return offer, f"{location} {place} {zone}", end


def csp_is_unpublished(ref: str) -> bool:
    """True si le site redirige explicitement vers « offre dépubliée » ; toute erreur réseau = on garde l'offre."""
    try:
        with requests.get(CSP_OFFER_URL.format(ref=ref), stream=True, timeout=TIMEOUT,
                          headers={"User-Agent": "Mozilla/5.0 (veille-pharmacie)"}) as r:
            return CSP_UNPUBLISHED_MARK in r.url
    except requests.RequestException:
        return False


def csp_download_and_scan() -> dict:
    """Télécharge le CSV en streaming dans un fichier temporaire, le lit ligne par ligne, le supprime."""
    res = csp_pick_resource()
    print(f"  Ressource : {res.get('title')} ({(res.get('filesize') or 0) / 1e6:.0f} Mo, "
          f"modifiée le {(res.get('last_modified') or '')[:10]})")
    fd, tmp_name = tempfile.mkstemp(prefix="csp_", suffix=".csv")
    tmp = Path(tmp_name)
    today = date.today()
    try:
        with os.fdopen(fd, "wb") as fh:
            try:
                with requests.get(res["url"], stream=True, timeout=TIMEOUT) as r:
                    if r.status_code != 200:
                        raise SourceError(f"Téléchargement CSP : HTTP {r.status_code}")
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            except requests.RequestException as e:
                raise http_error("Téléchargement CSP", e) from e

        csv.field_size_limit(10 ** 7)
        total = 0
        latest_pub = ""
        entries = []
        with open(tmp, encoding="utf-8-sig", newline="") as fh:  # utf-8-sig : tolère un éventuel BOM
            reader = csv.DictReader(fh, delimiter=";")
            columns = reader.fieldnames or []
            print(f"  Colonnes ({len(columns)}) : {' | '.join(columns)}")
            missing = [c for c in ("Référence", "Intitulé du poste", "Localisation du poste",
                                   "Date de fin de publication par défaut") if c not in columns]
            if missing:
                raise SourceError(f"CSP : colonnes attendues absentes ({', '.join(missing)}).")
            for row in reader:
                total += 1
                pub = parse_fr_date(row.get("Date de première publication") or "")
                if pub and pub != date.max and pub.isoformat() > latest_pub:
                    latest_pub = pub.isoformat()
                if not title_ok(row.get("Intitulé du poste") or ""):
                    continue
                end = parse_fr_date(row.get("Date de fin de publication par défaut") or "")
                if end is None or end < today:  # fichier cumulatif : seulement les offres encore ouvertes
                    continue
                offer, loc_text, _ = csp_convert(row)
                offer["_end"] = end.isoformat()
                offer["_loc"] = loc_text
                offer["_versant"] = (row.get("Versant") or "").strip() or (
                    "Fonction Publique Hospitalière (AP-HP, versant vide)" if offer["sourceId"].startswith("csp-APHP_")
                    else "(vide)")
                entries.append(offer)
    finally:
        tmp.unlink(missing_ok=True)
    # Le CSV ne reflète pas toujours le site : on écarte les offres que le site dit dépubliées.
    candidates = [o for o in entries if classify_location(o["_loc"]) != "out"]
    for o in candidates:
        if csp_is_unpublished(o["sourceId"][4:]):
            o["_unpublished"] = True
    return {
        "schema": CSP_SCHEMA,
        "fetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "resource": {"title": res.get("title"), "last_modified": res.get("last_modified")},
        "rawCount": total,
        "latestPublication": latest_pub,
        "offers": entries,
    }


def fetch_csp() -> tuple[list[tuple], int, str]:
    cache = None
    if CSP_CACHE.exists():
        try:
            cache = json.loads(CSP_CACHE.read_text(encoding="utf-8"))
            age = datetime.now().astimezone() - datetime.fromisoformat(cache["fetchedAt"])
            if cache.get("schema") != CSP_SCHEMA or age >= timedelta(days=CSP_CACHE_DAYS):
                cache = None
            else:
                print(f"  Cache data/csp.json récent ({age.days} j) : pas de téléchargement.")
        except (ValueError, KeyError, OSError):
            cache = None
    if cache is None:
        cache = csp_download_and_scan()
        CSP_CACHE.parent.mkdir(exist_ok=True)
        CSP_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    today = date.today().isoformat()
    out = []
    unpublished = 0
    versants: dict[str, str] = {}
    for entry in cache["offers"]:
        entry = dict(entry)
        end, loc_text = entry.pop("_end", ""), entry.pop("_loc", "")
        versant = entry.pop("_versant", "")
        if end and end < today:  # offre close depuis la mise en cache
            continue
        if entry.pop("_unpublished", False):
            unpublished += 1
            continue
        versants[entry["sourceId"]] = versant
        out.append((entry, loc_text))
    CSP_META.update(versants=versants, latest_publication=cache.get("latestPublication", ""), unpublished=unpublished)
    print(f"  CSV : {cache['rawCount']} lignes, ressource {cache['resource'].get('title')} ; "
          f"{len(cache['offers'])} offres ouvertes à titre « préparateur pharmacie » ; "
          f"{unpublished} écartée(s) car dépubliée(s) sur le site.")
    return out, cache["rawCount"], cache["fetchedAt"]


# --------------------------------------------------------------------------- #
# Dédoublonnage
# --------------------------------------------------------------------------- #

def is_empty(value: str) -> bool:
    return not value or value in (PLACEHOLDER, TO_CHECK)


def norm_place(location: str) -> str:
    """Lieu normalisé pour la clé de doublon : ville seule, sans code postal, n° de département, arrondissement ni région."""
    s = POSTAL_RE.sub(" ", location or "")
    s = PAREN_DEPT_RE.sub(" ", s)
    s = LEAD_DEPT_RE.sub(" ", s)
    segments = [seg for seg in re.split(r"\s*[,–]\s*", s) if fold_words(seg)]
    s = fold(segments[0]) if segments else ""
    s = re.sub(r"\b\d+\s*(?:er|e|eme)?\b", " ", s)
    return re.sub(r"\s*\bcedex\b.*", "", fold_words(s))


def dedup_key(o: dict) -> str:
    employer = "" if is_empty(o.get("employer", "")) else fold_words(o["employer"])
    title = re.sub(r"\b[hf]\s*/\s*[hf]\b", " ", fold(o.get("title", "")))  # marqueur H/F, F/H
    return "|".join([fold_words(title), employer, norm_place(o.get("location", ""))])


def jb_key(o: dict) -> str:
    """Doublon Jooble : intitulé normalisé + lieu + 80 premiers caractères de l'extrait normalisés."""
    return "|".join([fold_words(o.get("title", "")), fold_words(o.get("location", "")),
                     fold_words(o.get("note", ""))[:80]])


def source_rank(offer: dict) -> int:
    prefix = offer["sourceId"].split("-", 1)[0]
    return SOURCE_PRIORITY.index(prefix) if prefix in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def all_ids(o: dict) -> list[str]:
    return o.get("sourceIds") or [o["sourceId"]]


def merge_group(group: list[dict]) -> dict:
    group = sorted(group, key=source_rank)  # tri stable : CSP > FT > Adzuna > Jooble
    merged = dict(group[0])
    for field in ("title", "employer", "location", "contract", "date", "pay", "link", "note", "diploma"):
        for o in group:
            if not is_empty(o.get(field, "")):
                merged[field] = o[field]
                break
    if merged.get("diploma"):
        merged["status"] = TO_CHECK
    ids: list[str] = []
    for o in group:
        for i in all_ids(o):
            if i not in ids:
                ids.append(i)
    if len(ids) > 1:
        merged["sourceIds"] = ids
    if any(o["adequation"] == "Élevée" for o in group):
        merged["adequation"] = "Élevée"
    return merged


def dedup(offers: list[dict]) -> list[dict]:
    """Par sourceId (ou l'un des sourceIds), puis par intitulé + employeur + lieu."""
    groups: list[list[dict]] = []
    by_id: dict[str, int] = {}
    for o in offers:
        idx = next((by_id[i] for i in all_ids(o) if i in by_id), None)
        if idx is None:
            groups.append([o])
            idx = len(groups) - 1
        else:
            groups[idx].append(o)
        for i in all_ids(o):
            by_id[i] = idx

    def is_jb(g: list[dict]) -> bool:
        return all(i.startswith("jb-") for o in g for i in all_ids(o))

    # 1) hors Jooble : intitulé + employeur + lieu (CSP, France Travail)
    by_key: dict[str, int] = {}
    final: list[list[dict]] = []
    jooble: list[list[dict]] = []
    for g in groups:
        if is_jb(g):
            jooble.append(g)
            continue
        k = dedup_key(g[0])
        if k in by_key:
            final[by_key[k]].extend(g)
        else:
            by_key[k] = len(final)
            final.append(g)
    # 2) Jooble : entre eux, seulement si intitulé + lieu + début d'extrait sont identiques ;
    #    avec une autre source, rattachement par intitulé + employeur + lieu
    by_jb: dict[str, int] = {}
    jb_final: list[list[dict]] = []
    for g in jooble:
        k = dedup_key(g[0])
        if k in by_key:
            final[by_key[k]].extend(g)
            continue
        jk = jb_key(g[0])
        if jk in by_jb:
            jb_final[by_jb[jk]].extend(g)
        else:
            by_jb[jk] = len(jb_final)
            jb_final.append(g)
    final += jb_final
    return [merge_group(g) if len(g) > 1 else g[0] for g in final]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def load_previous() -> dict | None:
    if not OUTPUT.exists():
        return None
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return data if isinstance(data.get("offers"), list) else None
    except (ValueError, OSError):
        return None


def previous_for(prev: dict | None, prefix: str) -> list[dict]:
    if not prev:
        return []
    cutoff = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    return [o for o in prev["offers"]
            if any(i.startswith(prefix + "-") for i in all_ids(o)) and not (o.get("date") and o["date"] < cutoff)]


def run_source(prefix: str, fn):
    """Exécute une source. Renvoie (statut, offres filtrées, brut, après filtres, updatedAt)."""
    label = SOURCE_LABELS[prefix]
    print(f"\n[{label}]")
    try:
        result = fn()
        converted, raw_count = result[0], result[1]
        updated = result[2] if len(result) > 2 else datetime.now().astimezone().isoformat(timespec="seconds")
        kept, stats = apply_filters(converted)
        print(f"  {raw_count} brute(s) -> {len(kept)} après filtres "
              f"(écartées : {stats['title']} intitulé, {stats['location']} lieu, {stats['age']} > {MAX_AGE_DAYS} j)")
        return "ok", kept, raw_count, updated
    except SourceSkipped as e:
        print(f"  Ignorée : {e}.")
        return "skipped", [], 0, None
    except SourceError as e:
        print(f"  AVERTISSEMENT : {e}", file=sys.stderr)
        return "error", [], 0, None
    except Exception as e:  # source en panne inattendue : ne doit jamais faire tomber les autres
        print(f"  AVERTISSEMENT : erreur inattendue ({type(e).__name__}).", file=sys.stderr)
        return "error", [], 0, None


def main() -> int:
    if load_dotenv is not None and (ROOT / ".env").exists():
        load_dotenv(ROOT / ".env")
    prev = load_previous()
    prev_sources = (prev or {}).get("sources", {})

    fetchers = {"jb": fetch_jooble}
    if ENABLE_CSP:
        fetchers["csp"] = fetch_csp
    if ENABLE_FRANCE_TRAVAIL:
        fetchers = {"ft": fetch_france_travail, **fetchers}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    sources: dict[str, dict] = {}
    pool: list[dict] = []
    summary = {}
    for prefix in fetchers:
        state, kept, raw_count, updated = run_source(prefix, fetchers[prefix])
        label = SOURCE_LABELS[prefix]
        if state == "ok":
            pool.extend(kept)
            sources[label] = {"ok": True, "count": len(kept), "updatedAt": updated}
        else:
            old = previous_for(prev, prefix)
            if old:
                print(f"  -> {len(old)} offre(s) précédente(s) de cette source reprises depuis offres.json.")
            pool.extend(old)
            sources[label] = {"ok": False, "count": len(old),
                              "updatedAt": prev_sources.get(label, {}).get("updatedAt")}
        summary[label] = (state, raw_count, len(kept))

    if not any(s["ok"] for s in sources.values()):
        if not prev:
            print("\nERREUR : aucune source n'a fonctionné et il n'y a pas d'offres.json précédent.", file=sys.stderr)
            return 1
        print("\nAucune source n'a fonctionné : offres.json précédent conservé tel quel.", file=sys.stderr)
        return 0

    offers = dedup(pool)
    # doublons entre sources = identifiants de sources distinctes regroupés dans une même offre
    cross = sum(len({i.split("-", 1)[0] for i in all_ids(o)}) - 1 for o in offers)
    payload = {"updatedAt": now, "sources": sources, "offers": offers}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n=== Résumé ===")
    for prefix in fetchers:
        label = SOURCE_LABELS[prefix]
        state, raw_count, kept = summary[label]
        print(f"  {label:<28} {state:<8} brutes : {raw_count:<6} après filtres : {kept}")
    csp_ids = {i for o in offers for i in all_ids(o) if i.startswith("csp-")}
    dist: dict[str, int] = {}
    for sid in csp_ids:
        v = CSP_META["versants"].get(sid)
        if v:
            dist[v] = dist.get(v, 0) + 1
    if CSP_META["latest_publication"]:
        print(f"  CSP — répartition par versant des offres retenues : {dist or 'aucune'}")
        print(f"  CSP — première publication la plus récente du fichier : {CSP_META['latest_publication']}")
    print(f"Total après dédoublonnage : {len(offers)} -> {OUTPUT.name} ({cross} doublon(s) fusionné(s) entre sources)")
    for o in offers[:8]:
        print(f"  - [{o['sourceId'].split('-', 1)[0]}] {o['title']} | {o['employer']} | {o['location']} | "
              f"{o['contract']} | {o['date']} | {o['link']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
