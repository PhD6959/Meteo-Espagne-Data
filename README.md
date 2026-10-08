# Meteo-Espagne-Data

Données publiques de l'appli Météo Espagne (dépôt public, aucune donnée personnelle).

- `data/rb1-stations.json` — stations AEMET du secteur RB1
- `data/rb1-pluie.json` — mesures quotidiennes AEMET (pluie mm, tmin, tmax), depuis le 2 juillet 2025
- `data/rb1-alertes.json` — avis Meteoalerta AEMET (Aragón) et incidences DGT dans le secteur
- `data/etat.json` — dernière exécution et erreurs

Mise à jour toutes les 3 heures par `.github/workflows/maj-donnees.yml` (script `scripts/maj_donnees.py`).
Sources : AEMET OpenData, DGT DATEX2. La clé AEMET est un secret GitHub (`AEMET_API_KEY`).
