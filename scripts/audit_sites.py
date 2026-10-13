"""
Audit des sites d'emploi : lesquels peut-on utiliser pour la veille, et comment ?

Pour chaque site de la liste SITES, le script fait au maximum 2 requêtes :
  1. robots.txt  -> le site autorise-t-il les robots sur la page de recherche ?
  2. la page de recherche (seulement si robots.txt l'autorise) -> on regarde :
     - un flux RSS/Atom          (la meilleure option : prévu pour être lu par des programmes)
     - des données JobPosting    (format standard schema.org, souvent lisible proprement)
     - des annonces dans le HTML (lisibles mais fragiles)
     - une protection anti-robot (captcha, Cloudflare, DataDome...) -> on n'insiste pas

Il ne contourne aucune protection, s'identifie honnêtement et attend entre deux sites.
Le verdict est TECHNIQUE : avant d'utiliser un site, il faut aussi lire ses CGU
(le script affiche le lien s'il le trouve).

Usage :
    python scripts/audit_sites.py
Résultat : un tableau dans le terminal + le fichier rapport_sites.md
"""

import json
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "PharmaVeilleAudit/0.1 (outil personnel; +https://github.com/LevronEff/pharma-veille)"
TIMEOUT = 15
PAUSE_ENTRE_SITES = 3  # secondes, par politesse
MOT_CLE = re.compile(r"pr[ée]parat", re.I)

# Modifie librement cette liste : nom + URL d'une page de résultats "préparateur pharmacie" à Paris.
# Si une URL renvoie 404, ouvre le site dans ton navigateur, fais la recherche et colle l'URL obtenue.
SITES = [
    ("HelloWork", "https://www.hellowork.com/fr-fr/emploi/recherche.html?k=pr%C3%A9parateur+pharmacie&l=Paris"),
    ("Indeed", "https://fr.indeed.com/jobs?q=pr%C3%A9parateur+pharmacie&l=Paris"),
    ("Staffsanté", "https://www.staffsante.fr/offres-emploi-de-preparateur-en-pharmacie"),
    ("AP-HP recrutement", "https://recrutement.aphp.fr/jobs?q=pr%C3%A9parateur"),
    ("FHF emploi (hôpitaux)", "https://emploi.fhf.fr/"),
    ("Jobijoba", "https://www.jobijoba.com/fr/query/?what=pr%C3%A9parateur+pharmacie&where=Paris"),
    ("Meteojob", "https://www.meteojob.com/jobsearch/offers?what=pr%C3%A9parateur%20pharmacie&where=Paris"),
    ("Talent.com", "https://fr.talent.com/jobs?k=pr%C3%A9parateur+pharmacie&l=Paris"),
    ("Welcome to the Jungle", "https://www.welcometothejungle.com/fr/jobs?query=pr%C3%A9parateur%20pharmacie"),
    ("Tkcare", "https://www.tkcare.fr/"),
    ("Choisir le service public", "https://choisirleservicepublic.gouv.fr/nos-offres/"),
]

ANTI_ROBOT = ["captcha", "cf-chl", "challenge-platform", "datadome", "px-captcha",
              "access denied", "are you a robot", "êtes-vous un robot", "verify you are human"]


# ---------- analyse (fonctions pures, testables sans réseau) ----------

def trouver_flux(html, base):
    liens = re.findall(r"<link[^>]+>", html, re.I)
    flux = []
    for l in liens:
        if re.search(r"application/(rss|atom)\+xml", l, re.I):
            m = re.search(r'href=["\']([^"\']+)', l, re.I)
            if m:
                flux.append(urljoin(base, m.group(1)))
    return flux


def compter_jobposting(html):
    """Compte les offres au format schema.org JobPosting (JSON-LD + microdonnées)."""
    total = 0

    def parcourir(x):
        nonlocal total
        if isinstance(x, dict):
            t = x.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                total += 1
            for v in x.values():
                parcourir(v)
        elif isinstance(x, list):
            for v in x:
                parcourir(v)

    for bloc in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            parcourir(json.loads(bloc.strip()))
        except ValueError:
            pass
    total += len(re.findall(r'itemtype=["\']https?://schema\.org/JobPosting', html, re.I))
    return total


def detecter_anti_robot(status, html):
    if status in (401, 403, 429, 503):
        return True
    bas = html[:200000].lower()
    return any(m in bas for m in ANTI_ROBOT)


def trouver_cgu(html, base):
    for href, texte in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        cible = (href + " " + re.sub(r"<[^>]+>", "", texte)).lower()
        if re.search(r"cgu|conditions[- _]g[ée]n[ée]rales|conditions d.utilisation|terms", cible):
            return urljoin(base, href)
    return ""


def verdict(r):
    if r["erreur"]:
        return "⚠️ Inaccessible", r["erreur"]
    if r["robots_interdit"]:
        return "❌ Interdit par robots.txt", "Le site demande aux robots de ne pas lire cette page."
    if r["anti_robot"]:
        return "❌ Protection anti-robot", "Ne pas contourner : utiliser une alerte e-mail à la place."
    if r["status"] and r["status"] >= 400:
        return "⚠️ URL à corriger", f"HTTP {r['status']} : refais la recherche dans ton navigateur et colle l'URL."
    if r["flux"]:
        return "✅ Flux RSS/Atom", "Meilleure option : prévu pour être lu par un programme."
    if r["jobposting"]:
        return "🟡 Données JobPosting", f"{r['jobposting']} offre(s) structurée(s) : faisable, vérifier les CGU."
    if r["mots_cles"]:
        return "🟡 Annonces dans le HTML", "Lisible mais fragile (casse si le site change) ; vérifier les CGU."
    if r["json_integre"]:
        return "🟡 Données JSON intégrées", "Annonces probablement dans un bloc JSON de la page : faisable mais fragile."
    return "❌ Contenu en JavaScript", "Annonces chargées après coup : difficile sans navigateur automatisé."


# ---------- réseau ----------

def analyser(session, nom, url):
    r = dict(nom=nom, url=url, status=None, erreur="", robots_interdit=False, anti_robot=False,
             flux=[], jobposting=0, mots_cles=0, json_integre=False, cgu="", sitemaps=0)
    racine = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    # 1. robots.txt
    try:
        rob = session.get(racine + "/robots.txt", timeout=TIMEOUT)
        rp = RobotFileParser()
        if rob.status_code in (401, 403):
            rp.disallow_all = True
        elif rob.status_code == 200:
            rp.parse(rob.text.splitlines())
            r["sitemaps"] = len(re.findall(r"^\s*sitemap:", rob.text, re.I | re.M))
        else:
            rp.allow_all = True
        r["robots_interdit"] = not rp.can_fetch(USER_AGENT, url)
    except requests.RequestException as e:
        r["erreur"] = f"robots.txt : {type(e).__name__}"
        return r

    if r["robots_interdit"]:
        return r  # on respecte : pas de requête sur la page

    # 2. page de recherche
    try:
        p = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        r["erreur"] = f"page : {type(e).__name__}"
        return r
    html = p.text or ""
    r["status"] = p.status_code
    r["anti_robot"] = detecter_anti_robot(p.status_code, html)
    r["flux"] = trouver_flux(html, p.url)
    r["jobposting"] = compter_jobposting(html)
    r["mots_cles"] = len(MOT_CLE.findall(re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)))
    r["json_integre"] = bool(re.search(r"__NEXT_DATA__|__NUXT__|window\.__INITIAL_STATE__", html))
    r["cgu"] = trouver_cgu(html, p.url)
    return r


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"})
    resultats = []
    for i, (nom, url) in enumerate(SITES):
        if i:
            time.sleep(PAUSE_ENTRE_SITES)
        print(f"→ {nom}…", flush=True)
        r = analyser(session, nom, url)
        r["verdict"], r["detail"] = verdict(r)
        resultats.append(r)

    print("\n" + "=" * 70)
    for r in resultats:
        print(f"\n{r['nom']}\n  {r['verdict']} — {r['detail']}")
        if r["flux"]:
            print(f"  Flux : {r['flux'][0]}")
        if r["cgu"]:
            print(f"  CGU à lire : {r['cgu']}")

    lignes = ["# Audit des sites d'emploi", "",
              "Verdict technique uniquement : lire les CGU de chaque site avant de l'utiliser.", "",
              "| Site | Verdict | Détail | HTTP | Flux | JobPosting | Mentions « préparat… » | CGU |",
              "|---|---|---|---|---|---|---|---|"]
    for r in resultats:
        lignes.append(f"| {r['nom']} | {r['verdict']} | {r['detail']} | {r['status'] or '—'} | "
                      f"{r['flux'][0] if r['flux'] else '—'} | {r['jobposting']} | {r['mots_cles']} | "
                      f"{r['cgu'] or '—'} |")
    with open("rapport_sites.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")
    print("\nRapport écrit dans rapport_sites.md")


if __name__ == "__main__":
    main()
