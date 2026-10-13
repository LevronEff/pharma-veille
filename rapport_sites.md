# Audit des sites d'emploi

Verdict technique uniquement : lire les CGU de chaque site avant de l'utiliser.

| Site | Verdict | Détail | HTTP | Flux | JobPosting | Mentions « préparat… » | CGU |
|---|---|---|---|---|---|---|---|
| HelloWork | ❌ Interdit par robots.txt | Le site demande aux robots de ne pas lire cette page. | — | — | 0 | 0 | — |
| Indeed | ❌ Protection anti-robot | Ne pas contourner : utiliser une alerte e-mail à la place. | 403 | — | 0 | 0 | — |
| Staffsanté | ❌ Protection anti-robot | Ne pas contourner : utiliser une alerte e-mail à la place. | 200 | — | 0 | 766 | https://www.staffsante.fr/contenu/conditions-generales-dutilisation/ |
| AP-HP recrutement | ⚠️ Inaccessible | robots.txt : SSLError | — | — | 0 | 0 | — |
| FHF emploi (hôpitaux) | ❌ Contenu en JavaScript | Annonces chargées après coup : difficile sans navigateur automatisé. | 200 | — | 0 | 0 | — |
| Jobijoba | ❌ Interdit par robots.txt | Le site demande aux robots de ne pas lire cette page. | — | — | 0 | 0 | — |
| Meteojob | 🟡 Annonces dans le HTML | Lisible mais fragile (casse si le site change) ; vérifier les CGU. | 200 | — | 0 | 20 | https://www.meteojob.com/conditions |
| Talent.com | 🟡 Annonces dans le HTML | Lisible mais fragile (casse si le site change) ; vérifier les CGU. | 200 | — | 0 | 56 | https://fr.talent.com/terms-of-service |
| Welcome to the Jungle | ❌ Contenu en JavaScript | Annonces chargées après coup : difficile sans navigateur automatisé. | 200 | — | 0 | 0 | https://www.welcometothejungle.com/fr/pages/terms |
| Tkcare | ❌ Contenu en JavaScript | Annonces chargées après coup : difficile sans navigateur automatisé. | 200 | — | 0 | 0 | https://www.tkcare.fr/mentions-legales#terms |
| Choisir le service public | ❌ Contenu en JavaScript | Annonces chargées après coup : difficile sans navigateur automatisé. | 200 | — | 0 | 0 | https://choisirleservicepublic.gouv.fr/mentions-legales/ |
