# Veille emploi pharmacie

Outil de suivi des offres de préparateur·trice en pharmacie (Paris et petite couronne : 75, 92, 93, 94).
`index.html` est le tableau de suivi (les données de suivi restent dans le navigateur) ;
`scripts/fetch_offres.py` agrège chaque jour deux sources dans `offres.json` — Jooble et Choisir le service public (données ouvertes) —, que la page importe via le bouton
« Récupérer les nouvelles offres ». Chaque offre porte un badge de source (JB, CSP).

## Lancer le script en local

```bash
pip install -r requirements.txt
# créer un fichier .env (non versionné) contenant :
#   JOOBLE_API_KEY=...
# Choisir le service public n'a pas besoin de clé.
python scripts/fetch_offres.py
python -m http.server   # puis ouvrir http://localhost:8000
```

## Fonctionnement

- France Travail est désactivée (`ENABLE_FRANCE_TRAVAIL = False` dans le script, code conservé ; il faudrait `FT_CLIENT_ID` / `FT_CLIENT_SECRET` et un abonnement à l'API Offres d'emploi v2).
- Chaque source est indépendante : identifiants absents → source ignorée ; erreur → avertissement et reprise des
  offres de cette source présentes dans l'`offres.json` précédent (elles ne passent donc pas en « Expirée » à cause d'une panne).
  Le script n'échoue que si aucune source ne fonctionne et qu'il n'y a pas d'`offres.json` précédent.
- Filtres communs : intitulé contenant « préparat » et « pharma », sans apprenti / alternance / stage / stagiaire ;
  lieu en 75, 92, 93 ou 94 (lieu ambigu → conservé, statut « À vérifier »).
- Dédoublonnage par `sourceId`, puis par intitulé + employeur + lieu ; priorité CSP > Jooble.
- Format : `{"updatedAt", "sources": {nom: {"ok", "count", "updatedAt"}}, "offers": [...]}`.
- Choisir le service public : le CSV (~120 Mo, jeu data.gouv.fr « les-offres-diffusees-sur-choisir-le-service-public »)
  est téléchargé en streaming dans un fichier temporaire supprimé après lecture ; le résultat filtré est mis en cache
  dans `data/csp.json` et réutilisé pendant 7 jours.

## Automatisation GitHub

1. Dépôt → Settings → Secrets and variables → Actions : créer le secret `JOOBLE_API_KEY` (une source sans secret est simplement ignorée).
2. Le workflow `.github/workflows/veille.yml` s'exécute chaque jour à 4 h UTC (ou à la demande via « Run workflow ») et commite `offres.json` et `data/csp.json` s'ils ont changé.
3. Activer GitHub Pages : Settings → Pages → Source « Deploy from a branch », branche `main`, dossier `/ (root)`.

Offres issues de Jooble et de Choisir le service public (données ouvertes data.gouv.fr, Licence Ouverte 2.0).
Le CSV étant cumulatif et en retard sur le site, le script ne garde que les offres dont la date de fin n'est pas dépassée
et écarte celles que le site indique « dépubliées ».
