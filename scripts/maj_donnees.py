#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Météo Espagne — mise à jour automatique des données publiques (dépôt Meteo-Espagne-Data)
Version 1.0 — 8 octobre 2026 à 15:20

Exécuté par GitHub Actions (.github/workflows/maj-donnees.yml). Produit :
  data/rb1-pluie.json    : historique quotidien AEMET par station (fusion des 15 derniers jours)
  data/rb1-alertes.json  : avis météo AEMET (Meteoalerta, zone Aragón) + incidences DGT dans la zone RB1
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


def aemet_get(url, essais=3):
    """Appel AEMET en deux temps (URL de données puis données), avec gestion du 429."""
    for essai in range(essais):
        try:
            r = requests.get(url, headers={"api_key": API_KEY}, timeout=30)
            if r.status_code == 429:
                time.sleep(30 * (essai + 1))
                continue
            if r.status_code == 401:
                raise RuntimeError("clé API AEMET refusée (401)")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            meta = r.json()
            if "datos" not in meta:
                return None
            time.sleep(0.5)
            d = requests.get(meta["datos"], timeout=60)
            d.raise_for_status()
            return d
        except RuntimeError:
            raise
        except Exception as e:  # réseau, JSON, etc.
            if essai == essais - 1:
                raise
            time.sleep(5)
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
def maj_pluie():
    stations = charger("rb1-stations.json", {})["stations"]
    pluie = charger("rb1-pluie.json", {"rb": "RB1", "champs": ["prec_mm", "tmin", "tmax"], "obs": {}})
    fin = datetime.now(timezone.utc).date()
    debut = fin - timedelta(days=JOURS_A_RAFRAICHIR)
    fi = f"{debut:%Y-%m-%d}T00:00:00UTC"
    ff = f"{fin:%Y-%m-%d}T23:59:59UTC"
    ajouts = 0
    for s in stations:
        sid = s["id"]
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
            time.sleep(1)
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
    pluie["maj"] = maintenant()
    ecrire("rb1-pluie.json", pluie, compact=True)
    print(f"AEMET : {ajouts} nouveaux jours-station")


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


def main():
    if not API_KEY:
        erreurs.append("AEMET_API_KEY absente : historique et avis AEMET non mis à jour")
    else:
        try:
            maj_pluie()
        except Exception as e:
            erreurs.append(f"Historique AEMET : {e}")
    alertes = charger("rb1-alertes.json", {"avis": [], "routes": []})
    if API_KEY:
        try:
            alertes["avis"] = maj_avis()
            alertes["avis_maj"] = maintenant()
        except Exception as e:
            erreurs.append(f"Avis AEMET : {e}")
    try:
        alertes["routes"] = maj_routes()
        alertes["routes_maj"] = maintenant()
    except Exception as e:
        erreurs.append(f"DGT : {e}")
    ecrire("rb1-alertes.json", alertes)
    ecrire("etat.json", {"derniere_execution": maintenant(), "erreurs": erreurs})
    for e in erreurs:
        print("⚠️", e)
    # Échec du job seulement si rien n'a pu être mis à jour
    sys.exit(1 if len(erreurs) >= 3 else 0)


if __name__ == "__main__":
    main()
