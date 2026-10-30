#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Météo Espagne — mise à jour automatique des données publiques (dépôt Meteo-Espagne-Data)
Version 1.8 — 30 octobre 2026 à 06:04 — plan B des observations horaires et relecture du jour par station
  · observations horaires : si le lien « toutes stations » (/observacion/convencional/todas) échoue,
    repli station par station (/observacion/convencional/datos/estacion/{id}) ; échec seulement si aucune ne répond
  · relecture quotidienne de l'historique suivie station par station (rb1-pluie.json « rafraichi » : {station: date}) ;
    seules les stations en échec ou en retard sont redemandées dans la journée (remplace « rafraichi_le » de la v1.7)
v1.7 — 29 octobre 2026 à 06:24 — moins de requêtes AEMET et règle d'échec corrigée
  · historique : seules les stations en retard sont interrogées (dernier jour < aujourd'hui − RETARD_AEMET_JOURS) ;
    toutes les stations restent rafraîchies une fois par jour (UTC), au cas où AEMET corrige des valeurs
  · règle d'échec : les stations sans aucun historique (ex. Bujaraloz 9576C) ne comptent plus ; échec si plus
    de la moitié des stations ayant un historique ont 2 jours ou plus de retard sur la station la plus à jour
v1.6 — 28 octobre 2026 à 08:08 — avis Meteoalerta mieux protégés
  · avis récupérés juste après les observations horaires, avant les stations (ils profitent des nouveaux essais 401)
  · un échec des avis ne fait échouer la tâche que si les derniers avis obtenus ont plus de 12 h (AVIS_TOLERANCE)
v1.5 — 28 octobre 2026 à 06:57 — AEMET très instable depuis le 27/10 (erreurs 500 et 401)
  · cadence limitée à 30 requêtes AEMET par minute (FAQ AEMET OpenData §3.5 : 40 connexions/minute/utilisateur)
  · nouvel essai sur les erreurs serveur (5xx) avec pauses de 15 puis 30 s, plafonné à 10 par exécution
  · etat.json : erreurs_horaire reflète enfin les échecs des observations horaires lors d'une mise à jour complète
  · échec de la tâche (et mail GitHub) seulement si : observations horaires, avis ou DGT en échec,
    ou plus de la moitié des stations RB1 sans données
v1.4 — 22 octobre 2026 à 21:19 — rétablit le plafond de 12 nouveaux essais 401 par exécution,
  absent de la v1.3 effectivement publiée (commit 8f406ba)
v1.3 — 22 octobre 2026 à 18:58 — nouvel essai sur refus AEMET (HTTP 401)
  · AEMET renvoie parfois 401 au hasard avec une clé valide (13 stations sur 67 le 22/10/2026 côté ancien
    outil) : jusqu'à 3 nouveaux essais espacés (10, 20, 30 s) avant de compter l'erreur,
    dans la limite de 12 nouveaux essais par exécution
v1.2 — 22 octobre 2026 à 18:42 — mise à jour complète déclenchée par l'ancienneté de la précédente
  · GitHub saute une grande partie des lancements programmés (~5 par jour au lieu de 24) : la mise à jour
    complète ne dépend plus de l'heure du lancement (heure UTC multiple de 3) mais se fait dès que la
    précédente date d'au moins 2 h 50 (ou si FORCER_TOUT=1, ou si etat.json est absent/illisible)
v1.2 (première) — 21 octobre 2026 à 07:40 — nouvel essai sur les 401 (20 s puis 40 s), commit a539fa6 ;
  écrasée par erreur par la v1.2 du 22/10 ci-dessus (préparée à partir de la v1.1), le principe est repris en v1.3
v1.1 — 9 octobre 2026 à 10:41 — ajout des observations horaires AEMET (mesures provisoires)
  · data/rb1-obs-horaires.json : pluie horaire par station (prec = mm des 60 min précédant fint, UTC), 15 jours glissants
  · exécution horaire ; historique quotidien, avis et DGT au plus toutes les 3 heures (ou si FORCER_TOUT=1)
v1.0 — 8 octobre 2026 à 15:20

Exécuté par GitHub Actions (.github/workflows/maj-donnees.yml). Produit :
  data/rb1-pluie.json    : historique quotidien AEMET par station (fusion des 15 derniers jours)
  data/rb1-alertes.json  : avis météo AEMET (Meteoalerta, zone Aragón) + incidences DGT dans la zone RB1
  data/rb1-obs-horaires.json : observations horaires AEMET (/observacion/convencional/todas, 12 dernières heures)
  data/etat.json         : horodatage et erreurs éventuelles de la dernière exécution

La clé AEMET est lue dans la variable d'environnement AEMET_API_KEY (secret GitHub),
jamais écrite dans le dépôt.

Sources :
  AEMET OpenData  /api/valores/climatologicos/diarios/datos/fechaini/{}/fechafin/{}/estacion/{}
                  /api/avisos_cap/ultimoelaborado/area/62   (62 = Aragón, spec officielle AEMET)
  DGT DATEX2 v3.6 https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v36.xml
                  (parsing repris de aemet_roadbook_update.py v2.7.4)

Conversion des valeurs identique à aemet_tracker.py : 'Ip' (inappréciable) = 0.1 mm, virgule → point.
"""

import io
import json
import os
import re
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://opendata.aemet.es/opendata/api"
DGT_URL = "https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v36.xml"
AVISOS_AREA = "62"          # Aragón
JOURS_A_RAFRAICHIR = 15     # AEMET publie avec quelques jours de retard et corrige parfois après coup
MARGE_BBOX = 0.15           # degrés autour du tracé pour retenir avis et incidences

ICI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(ICI, "..", "data"))
API_KEY = os.environ.get("AEMET_API_KEY", "").strip()

# Emprise du tracé RB1 (calculée sur RB1_4_étapes.gpx, 547 points) + marge
BBOX = {"min_lat": 41.5184 - MARGE_BBOX, "max_lat": 42.50478 + MARGE_BBOX,
        "min_lon": -0.76806 - MARGE_BBOX, "max_lon": 0.51176 + MARGE_BBOX}

erreurs = []


def maintenant():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def charger(nom, defaut):
    chemin = os.path.join(DATA, nom)
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    return defaut


def ecrire(nom, obj, compact=False):
    chemin = os.path.join(DATA, nom)
    with open(chemin, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)


INTERVALLE_AEMET = 2.0   # v1.5 : secondes minimum entre deux requêtes AEMET (≤ 30/min, limite AEMET 40/min)
_derniere_requete = [0.0]
budget_5xx = [10]        # v1.5 : plafond de nouveaux essais sur erreur serveur AEMET par exécution


def get_aemet(url, **kw):
    """requests.get vers AEMET en respectant INTERVALLE_AEMET entre deux requêtes (API et URL de données)."""
    attente = _derniere_requete[0] + INTERVALLE_AEMET - time.monotonic()
    if attente > 0:
        time.sleep(attente)
    try:
        return requests.get(url, **kw)
    finally:
        _derniere_requete[0] = time.monotonic()


RETRY_401 = 3   # v1.3 : nouveaux essais après un refus 401 (pauses de 10, 20, 30 s)
budget_401 = [12]  # v1.4 : plafond de nouveaux essais 401 par exécution (≈ 4 min de pauses au plus),
                   # pour rester loin du délai de 20 min de la tâche GitHub si la clé est vraiment refusée


def aemet_get(url, essais=3):
    """Appel AEMET en deux temps (URL de données puis données), avec gestion du 429 et des 401 isolés."""
    refus = 0
    essai = 0
    while essai < essais:
        try:
            r = get_aemet(url, headers={"api_key": API_KEY}, timeout=30)
            if r.status_code == 401:
                if refus < RETRY_401 and budget_401[0] > 0:
                    refus += 1
                    budget_401[0] -= 1
                    time.sleep(10 * refus)
                    continue          # un refus 401 ne consomme pas d'essai réseau
                raise RuntimeError(f"clé API AEMET refusée (401, {refus + 1} essais)")
            if r.status_code == 429:
                time.sleep(30 * (essai + 1))
                essai += 1
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            meta = r.json()
            if "datos" not in meta:
                return None
            d = get_aemet(meta["datos"], timeout=60)
            d.raise_for_status()
            return d
        except RuntimeError:
            raise
        except Exception as e:  # réseau, JSON, erreurs serveur 5xx, etc.
            serveur = isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code >= 500
            if essai == essais - 1 or (serveur and budget_5xx[0] <= 0):
                raise
            if serveur:
                budget_5xx[0] -= 1
            essai += 1
            time.sleep(15 * essai if serveur else 5)
    return None


def nombre(v, prec=False):
    if v is None or v == "":
        return None
    s = str(v).strip()
    if prec and s == "Ip":
        return 0.1
    try:
        return round(float(s.replace(",", ".")), 1)
    except ValueError:
        return None  # 'Acum', 'Varias', etc. : non numérique, laissé vide


# ---------------------------------------------------------------- historique AEMET
RETARD_AEMET_JOURS = 4   # v1.7 : décalage de publication AEMET (« retardo de 4 días », métadonnées AEMET)


def maj_pluie():
    stations = charger("rb1-stations.json", {})["stations"]
    pluie = charger("rb1-pluie.json", {"rb": "RB1", "champs": ["prec_mm", "tmin", "tmax"], "obs": {}})
    fin = datetime.now(timezone.utc).date()
    debut = fin - timedelta(days=JOURS_A_RAFRAICHIR)
    fi = f"{debut:%Y-%m-%d}T00:00:00UTC"
    ff = f"{fin:%Y-%m-%d}T23:59:59UTC"
    cible = f"{fin - timedelta(days=RETARD_AEMET_JOURS):%Y-%m-%d}"
    aujourd_hui = f"{fin:%Y-%m-%d}"
    pluie.pop("rafraichi_le", None)                       # v1.8 : remplacé par le suivi par station
    rafraichi = pluie.setdefault("rafraichi", {})         # {station: date UTC de la dernière relecture réussie}
    ajouts = 0
    demandees = 0
    for s in stations:
        sid = s["id"]
        hist = pluie["obs"].get(sid) or {}
        if rafraichi.get(sid) == aujourd_hui and hist and max(hist) >= cible:
            continue            # v1.8 : déjà relue aujourd'hui et à jour, inutile d'interroger AEMET
        demandees += 1
        variantes = [sid]
        if sid[-1].isalpha():  # même logique que aemet_tracker.py
            variantes.append(sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        lignes = None
        for code in variantes:
            try:
                rep = aemet_get(f"{BASE_URL}/valores/climatologicos/diarios/datos/fechaini/{fi}/fechafin/{ff}/estacion/{code}")
            except Exception as e:
                erreurs.append(f"AEMET station {sid} : {e}")
                rep = None
            if rep is not None:
                try:
                    lignes = rep.json()
                except ValueError:
                    lignes = None
            if lignes:
                break
        if not lignes:
            continue
        obs = pluie["obs"].setdefault(sid, {})
        for l in lignes:
            date = str(l.get("fecha", ""))[:10]
            if not date:
                continue
            val = [nombre(l.get("prec"), prec=True), nombre(l.get("tmin")), nombre(l.get("tmax"))]
            if date not in obs:
                ajouts += 1
            obs[date] = val
        pluie["obs"][sid] = dict(sorted(obs.items()))
        rafraichi[sid] = aujourd_hui
    pluie["maj"] = maintenant()
    ecrire("rb1-pluie.json", pluie, compact=True)
    # v1.7 : retard de chaque station ayant un historique, par rapport à la station la plus à jour
    derniers = {sid: max(o) for sid, o in pluie["obs"].items() if o}
    en_retard = 0
    if derniers:
        ref = datetime.strptime(max(derniers.values()), "%Y-%m-%d").date()
        en_retard = sum(1 for d in derniers.values() if (ref - datetime.strptime(d, "%Y-%m-%d").date()).days >= 2)
    print(f"AEMET : {demandees} station(s) interrogée(s), "
          f"{ajouts} nouveaux jours-station, {en_retard} station(s) sur {len(derniers)} avec 2 jours ou plus de retard")
    return en_retard, len(derniers)


# ---------------------------------------------------------------- observations horaires (v1.1)
JOURS_HORAIRES = 15

def maj_obs_horaires():
    """Pluie horaire des stations RB1. Métadonnées AEMET : prec = « Precipitación acumulada, medida por el
    pluviómetro, durante los 60 minutos anteriores a la hora indicada por 'fint' (mm) », fint en UTC."""
    ids = {s["id"] for s in charger("rb1-stations.json", {})["stations"]}
    try:
        rep = aemet_get(f"{BASE_URL}/observacion/convencional/todas")
        if rep is None:
            raise RuntimeError("aucune donnée d'observation renvoyée")
        releves = rep.json()
    except Exception as e:                       # v1.8 : plan B station par station
        print(f"⚠️  Observations « toutes stations » indisponibles ({e}) : repli station par station")
        releves, ok = [], 0
        for sid in sorted(ids):
            try:
                r = aemet_get(f"{BASE_URL}/observacion/convencional/datos/estacion/{sid}")
                if r is not None:
                    releves += r.json()
                    ok += 1
            except Exception:
                pass
        if not ok:
            raise RuntimeError(f"toutes stations : {e} ; repli station par station : aucune réponse")
        print(f"   repli : {ok} station(s) sur {len(ids)} ont répondu")
    obs = charger("rb1-obs-horaires.json", {"rb": "RB1", "unite": "mm sur les 60 min précédant l'heure (UTC)", "obs": {}})
    limite = (datetime.now(timezone.utc) - timedelta(days=JOURS_HORAIRES)).strftime("%Y-%m-%dT%H:00Z")
    nouv = 0
    for o in releves:
        sid = o.get("idema")
        if sid not in ids or o.get("prec") is None or not o.get("fint"):
            continue
        h = o["fint"][:13] + ":00Z"          # 2026-10-09T07:00:00+0000 -> 2026-10-09T07:00Z
        if h < limite:
            continue
        st = obs["obs"].setdefault(sid, {})
        if h not in st:
            nouv += 1
        st[h] = round(float(o["prec"]), 1)
    for sid in list(obs["obs"]):
        obs["obs"][sid] = dict(sorted((h, v) for h, v in obs["obs"][sid].items() if h >= limite))
    obs["maj"] = maintenant()
    ecrire("rb1-obs-horaires.json", obs, compact=True)
    print(f"Observations horaires : {nouv} nouvelles heures-station")


# ---------------------------------------------------------------- avis Meteoalerta
CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}


def polygone_dans_zone(texte):
    for paire in texte.split():
        try:
            lat, lon = (float(x) for x in paire.split(",")[:2])
        except ValueError:
            continue
        if BBOX["min_lat"] <= lat <= BBOX["max_lat"] and BBOX["min_lon"] <= lon <= BBOX["max_lon"]:
            return True
    return False


def lire_cap(xml_bytes, maintenant_utc):
    avis = []
    root = ET.fromstring(xml_bytes)
    infos = root.findall("cap:info", CAP_NS)
    # Une info par langue : on garde l'espagnol, à défaut la première
    choix = [i for i in infos if (i.findtext("cap:language", "", CAP_NS) or "").lower().startswith("es")] or infos[:1]
    for info in choix:
        fin = info.findtext("cap:expires", "", CAP_NS)
        try:
            if fin and datetime.fromisoformat(fin) < maintenant_utc:
                continue
        except ValueError:
            pass
        niveau = ""
        for p in info.findall("cap:parameter", CAP_NS):
            if "nivel" in (p.findtext("cap:valueName", "", CAP_NS) or "").lower():
                niveau = p.findtext("cap:value", "", CAP_NS)
        for area in info.findall("cap:area", CAP_NS):
            poly = " ".join(x.text or "" for x in area.findall("cap:polygon", CAP_NS))
            if not polygone_dans_zone(poly):
                continue
            avis.append({
                "zone": area.findtext("cap:areaDesc", "", CAP_NS),
                "evenement": info.findtext("cap:event", "", CAP_NS),
                "niveau": niveau,
                "gravite": info.findtext("cap:severity", "", CAP_NS),
                "debut": info.findtext("cap:onset", "", CAP_NS) or info.findtext("cap:effective", "", CAP_NS),
                "fin": fin,
                "titre": info.findtext("cap:headline", "", CAP_NS),
                "description": info.findtext("cap:description", "", CAP_NS),
            })
    return avis


def maj_avis():
    rep = aemet_get(f"{BASE_URL}/avisos_cap/ultimoelaborado/area/{AVISOS_AREA}")
    if rep is None:
        return []
    contenu = rep.content
    t = datetime.now(timezone.utc)
    avis = []
    try:  # le produit est normalement une archive tar de fichiers CAP
        with tarfile.open(fileobj=io.BytesIO(contenu), mode="r:*") as tar:
            for m in tar.getmembers():
                if m.isfile() and m.name.lower().endswith(".xml"):
                    avis += lire_cap(tar.extractfile(m).read(), t)
    except tarfile.ReadError:
        if contenu.lstrip().startswith(b"<"):
            avis = lire_cap(contenu, t)
        else:
            raise RuntimeError("format d'avis AEMET inattendu")
    vus, uniques = set(), []
    for a in avis:
        cle = (a["zone"], a["evenement"], a["debut"], a["fin"])
        if cle not in vus:
            vus.add(cle)
            uniques.append(a)
    print(f"AEMET avis : {len(uniques)} dans la zone RB1")
    return uniques


# ---------------------------------------------------------------- incidences DGT
def maj_routes():
    r = requests.get(DGT_URL, timeout=90)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {
        "sit": "http://levelC/schema/3/situation",
        "loc": "http://levelC/schema/3/locationReferencing",
        "lse": "http://levelC/schema/3/locationReferencingSpanishExtension",
    }
    routes = []
    for situation in root.findall(".//sit:situation", ns):
        sev = situation.findtext("sit:overallSeverity", "unknown", ns)
        for rec in situation.findall(".//sit:situationRecord", ns):
            mgmt = rec.find(".//sit:roadOrCarriagewayOrLaneManagementType", ns)
            if mgmt is None:
                continue
            coords = []
            for bloc in ("from", "to", "point"):
                lat = rec.find(f".//loc:{bloc}//loc:latitude", ns)
                lon = rec.find(f".//loc:{bloc}//loc:longitude", ns)
                if lat is not None and lon is not None:
                    coords.append([float(lat.text), float(lon.text)])
            if not coords or not any(BBOX["min_lat"] <= a <= BBOX["max_lat"] and BBOX["min_lon"] <= o <= BBOX["max_lon"] for a, o in coords):
                continue
            detail = rec.findtext(".//sit:poorWeatherConditionsType", None, ns) or rec.findtext(".//sit:roadMaintenanceType", None, ns)
            pk1 = rec.findtext(".//loc:from//lse:kilometerPoint", "", ns)
            pk2 = rec.findtext(".//loc:to//lse:kilometerPoint", "", ns)
            routes.append({
                "route": rec.findtext(".//loc:roadName", "N/A", ns),
                "pk": f"{pk1}-{pk2}" if pk1 and pk2 else pk1,
                "gestion": mgmt.text,
                "cause": rec.findtext(".//sit:causeType", "", ns),
                "detail": detail,
                "gravite": sev,
                "depuis": (rec.findtext("sit:situationRecordCreationTime", "", ns) or "")[:16],
                "province": rec.findtext(".//lse:province", None, ns),
                "coords": coords,
            })
    ordre = {"highest": 0, "high": 1, "medium": 2, "low": 3}
    routes.sort(key=lambda x: ordre.get(x["gravite"], 4))
    print(f"DGT : {len(routes)} incidences dans la zone RB1")
    return routes


AVIS_TOLERANCE = timedelta(hours=12)   # v1.6 : âge maximal des avis avant qu'un échec soit bloquant


def trop_vieux(horodatage, tolerance):
    """Vrai si l'horodatage « AAAA-MM-JJTHH:MMZ » est absent, illisible ou plus vieux que la tolérance."""
    try:
        t = datetime.strptime(horodatage or "", "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - t > tolerance


DELAI_COMPLET = timedelta(hours=2, minutes=50)   # marge de 10 min sous 3 h pour absorber les retards de lancement


def complet_du():
    """Vrai si la dernière mise à jour complète date d'au moins DELAI_COMPLET (ou est inconnue)."""
    try:
        der = datetime.strptime(charger("etat.json", {}).get("derniere_execution", ""), "%Y-%m-%dT%H:%MZ")
    except (ValueError, TypeError, json.JSONDecodeError):
        return True
    return datetime.now(timezone.utc) - der.replace(tzinfo=timezone.utc) >= DELAI_COMPLET


def main():
    tout = os.environ.get("FORCER_TOUT") == "1" or complet_du()
    critique = []           # v1.5 : échecs qui justifient un échec de la tâche (et un mail GitHub)
    if not API_KEY:
        erreurs.append("AEMET_API_KEY absente : données AEMET non mises à jour")
        critique.append("clé absente")
    else:
        try:
            maj_obs_horaires()
        except Exception as e:
            erreurs.append(f"Observations horaires AEMET : {e}")
            critique.append("observations horaires")
    erreurs_horaire = list(erreurs)
    if not tout:            # passage horaire léger : observations seulement
        etat = charger("etat.json", {})
        etat["derniere_execution_horaire"] = maintenant()
        etat["erreurs_horaire"] = erreurs_horaire
        ecrire("etat.json", etat)
        for e in erreurs:
            print("⚠️", e)
        sys.exit(1 if critique else 0)
    alertes = charger("rb1-alertes.json", {"avis": [], "routes": []})
    if API_KEY:             # v1.6 : avis avant les stations, pour qu'ils profitent des nouveaux essais 401
        try:
            alertes["avis"] = maj_avis()
            alertes["avis_maj"] = maintenant()
        except Exception as e:
            erreurs.append(f"Avis AEMET : {e}")
            if trop_vieux(alertes.get("avis_maj"), AVIS_TOLERANCE):
                critique.append("avis (derniers obtenus il y a plus de 12 h)")
            else:
                print(f"ℹ️  Avis non mis à jour, derniers obtenus le {alertes.get('avis_maj')} : échec non bloquant")
    if API_KEY:
        try:
            retard, total = maj_pluie()
            if retard * 2 > total:
                critique.append(f"historique : {retard} stations sur {total} avec 2 jours ou plus de retard")
        except Exception as e:
            erreurs.append(f"Historique AEMET : {e}")
            critique.append("historique")
    try:
        alertes["routes"] = maj_routes()
        alertes["routes_maj"] = maintenant()
    except Exception as e:
        erreurs.append(f"DGT : {e}")
        critique.append("DGT")
    ecrire("rb1-alertes.json", alertes)
    ecrire("etat.json", {"derniere_execution": maintenant(), "erreurs": erreurs,
                         "derniere_execution_horaire": maintenant(), "erreurs_horaire": erreurs_horaire})
    for e in erreurs:
        print("⚠️", e)
    if critique:
        print("❌ Échec :", ", ".join(critique))
    # v1.5→1.7 : échec si observations ou DGT échouent, avis de plus de 12 h, ou plus de la moitié des stations en retard, ou si plus de la moitié des stations RB1 manquent
    sys.exit(1 if critique else 0)


if __name__ == "__main__":
    main()
