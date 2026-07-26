"""
Blueprint de l assistant d import Traccar : page de parametrage, recuperation de la config
Traccar courante (pour pre-remplir le formulaire), proxy de requetes vers un serveur Traccar
cible (contourne les restrictions CORS), et historique des imports realises.

Extrait de dashboard.py. Depend de TRACCAR_URL/TRACCAR_USER/TRACCAR_PASSWORD (mutables,
rechargeables via /api/config_traccar) et de app.template_folder, references via
"import dashboard" (meme motif que ndvi_bp.py) pour toujours voir leur valeur a jour.
"""
import os
import re
import json
import sqlite3
from datetime import datetime

import requests
from flask import Blueprint, request, jsonify, send_file, render_template, redirect, url_for, session as flask_session

import dashboard

traccar_import_bp = Blueprint("traccar_import", __name__)


@traccar_import_bp.before_request
def _require_login():
    """
    Meme authentification que le reste de l'application -- avec la meme distinction que le
    decorateur login_required d'origine : seules les routes /api/... renvoient du JSON,
    /traccar_import (page) redirige vers la connexion comme les autres pages.
    """
    if not flask_session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401
        return redirect(url_for("login"))


@traccar_import_bp.route("/traccar_import")
def traccar_import():
    import os
    for p in [os.path.join(dashboard.app.template_folder or "templates", "traccar_import.html"),
              os.path.join(os.path.dirname(__file__), "traccar_import.html"),
              "traccar_import.html"]:
        if os.path.isfile(p):
            return send_file(p)
    return render_template("traccar_import.html")

@traccar_import_bp.route("/api/traccar_import_config")
def api_traccar_import_config():
    """
    Expose la configuration Traccar déjà enregistrée côté serveur (config.json), pour
    pré-remplir automatiquement la page /traccar_import et éviter de ressaisir l'URL et
    les identifiants à chaque utilisation. Protégé par la même authentification que le
    reste du dashboard (aucune exposition supplémentaire : le mot de passe est déjà stocké
    en clair côté serveur pour l'usage interne de l'application).

    IMPORTANT : dashboard.TRACCAR_URL est stocké avec le suffixe "/api" (ex: ".../8082/api"), car
    c'est la convention utilisée par les appels serveur->serveur de dashboard.py. Mais la
    page /traccar_import ajoute elle-même "/api/..." a chaque requete (voir TraccarAPI._req),
    donc elle attend une URL DE BASE sans "/api". Sans ce retrait, l'URL pre-remplie pointait
    vers ".../api/api/devices" -> 404 Not Found.
    """
    base_url = re.sub(r"/api/?$", "", dashboard.TRACCAR_URL, flags=re.IGNORECASE)
    return jsonify({"url": base_url, "user": dashboard.TRACCAR_USER, "password": dashboard.TRACCAR_PASSWORD})


@traccar_import_bp.route("/api/traccar_proxy", methods=["POST"])
def api_traccar_proxy():
    """
    Relaie une requête vers un serveur Traccar (utilisé par /traccar_import). Nécessaire
    car un appel direct navigateur -> Traccar est bloqué par le navigateur si le serveur
    Traccar n'envoie pas d'en-têtes CORS (Access-Control-Allow-Origin) -- ce qui est le cas
    par défaut sur la plupart des installations Traccar. En passant par le serveur Flask
    (requête serveur -> serveur, comme le fait deja tout le reste du dashboard), le probleme
    de CORS disparait entierement : le navigateur ne dialogue qu'avec sa propre origine.
    Corps attendu (JSON) : {"url": "<url complete cible>", "method": "GET/POST/DELETE",
    "authHeader": "Basic ..." ou "Bearer ...", "body": <objet optionnel>}
    """
    data = request.get_json(silent=True) or {}
    target_url = data.get("url", "")
    method = str(data.get("method", "GET")).upper()
    auth_header = data.get("authHeader")
    body = data.get("body")

    if not target_url or not (target_url.startswith("http://") or target_url.startswith("https://")):
        return jsonify({"error": "url cible invalide"}), 400
    if method not in ("GET", "POST", "PUT", "DELETE"):
        return jsonify({"error": "methode non autorisee"}), 400

    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    try:
        resp = requests.request(method, target_url, headers=headers,
                                 json=body if body is not None else None, timeout=15)
    except Exception as e:
        return jsonify({"error": f"Connexion impossible : {e}"}), 502

    try:
        payload = resp.json()
    except Exception:
        payload = resp.text

    return jsonify({"status": resp.status_code, "body": payload})


def _ensure_import_history_table():
    with sqlite3.connect('database.db') as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traccar_import_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                traccar_url TEXT,
                nb_created INTEGER DEFAULT 0,
                nb_reused INTEGER DEFAULT 0,
                nb_errors INTEGER DEFAULT 0,
                summary TEXT
            )
        """)


@traccar_import_bp.route("/api/traccar_import_history", methods=["GET", "POST"])
def api_traccar_import_history():
    """
    Historique des imports lancés depuis /traccar_import : persisté côté serveur (et non
    dans le navigateur) pour rester consultable même en changeant de poste. GET renvoie
    les 30 derniers imports ; POST en enregistre un nouveau (appelé automatiquement à la
    fin de chaque import par la page).
    """
    _ensure_import_history_table()
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            conn.execute(
                "INSERT INTO traccar_import_history (timestamp, traccar_url, nb_created, nb_reused, nb_errors, summary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.utcnow().isoformat(), data.get("url", ""),
                 int(data.get("created", 0) or 0), int(data.get("reused", 0) or 0),
                 int(data.get("errors", 0) or 0), data.get("summary", ""))
            )
            conn.commit()
            return jsonify({"status": "ok"})
        cur = conn.execute("SELECT * FROM traccar_import_history ORDER BY id DESC LIMIT 30")
        return jsonify([dict(r) for r in cur.fetchall()])


def _geojson_polygon_vers_wkt_traccar(coords):
    """
    Convertit un anneau exterieur de polygone GeoJSON (liste de [lon, lat]) au format WKT
    attendu par Traccar pour une geofence : "POLYGON ((lat lon, lat lon, ...))". Traccar
    stocke ses geofences avec l'ordre (lat, lon), et NON l'ordre standard OGC/GeoJSON
    (lon, lat) -- meme convention que celle deja documentee dans dashboard._parse_geofence_wkt
    pour la lecture inverse (WKT Traccar -> GeoJSON).
    """
    paires = ", ".join(f"{lat} {lon}" for lon, lat in coords)
    return f"POLYGON (({paires}))"


@traccar_import_bp.route("/api/rpg_search")
def api_rpg_search():
    """
    Recherche les parcelles agricoles du Registre Parcellaire Graphique (RPG, IGN/ASP)
    pour une annee (millesime RPG) donnee. Sert de proxy serveur->serveur vers l'API Carto
    de l'IGN (https://apicarto.ign.fr/api/rpg/v2), publique et sans cle, pour eviter tout
    probleme de CORS cote navigateur (meme motif que /api/traccar_proxy pour Traccar).

    Deux modes d'interrogation, au choix :
      - lat + lon : recherche ponctuelle (une parcelle precise, si on connait deja un point
        dedans).
      - bbox=ouest,sud,est,nord (WGS84) : recherche par zone -- renvoie TOUTES les parcelles
        RPG dont la geometrie intersecte cette zone, pour un affichage carte (zone
        correspondant typiquement a l'emprise visible d'une carte Leaflet cote client).

    annee (millesime RPG, 2015 ou plus recent -- la version 2 du RPG, seule geree ici) est
    requis dans les deux cas. code_cultu (optionnel) filtre par code culture RPG.

    Retourne une liste simplifiee de parcelles trouvees, avec leur geometrie deja
    convertie au format WKT attendu par Traccar (voir _geojson_polygon_vers_wkt_traccar),
    prete a etre envoyee telle quelle a POST /geofences via /api/traccar_proxy, ainsi que
    la geometrie GeoJSON brute pour affichage carte cote client.
    """
    try:
        annee = int(request.args.get("annee"))
    except (TypeError, ValueError):
        return jsonify({"error": "Parametre annee requis (entier, ex 2023)"}), 400
    if annee < 2015:
        return jsonify({"error": "Seuls les millesimes RPG a partir de 2015 sont geres par cette recherche (version 2 du RPG)"}), 400

    bbox_raw = request.args.get("bbox")
    if bbox_raw:
        try:
            ouest, sud, est, nord = [float(v) for v in bbox_raw.split(",")]
        except (TypeError, ValueError):
            return jsonify({"error": "bbox invalide, format attendu : ouest,sud,est,nord"}), 400
        geom = json.dumps({
            "type": "Polygon",
            "coordinates": [[[ouest, sud], [est, sud], [est, nord], [ouest, nord], [ouest, sud]]],
        })
    else:
        try:
            lat = float(request.args.get("lat"))
            lon = float(request.args.get("lon"))
        except (TypeError, ValueError):
            return jsonify({"error": "Parametres lat/lon (recherche ponctuelle) ou bbox (recherche par zone) requis"}), 400
        geom = json.dumps({"type": "Point", "coordinates": [lon, lat]})

    code_cultu = request.args.get("code_cultu", "").strip()
    params = {"geom": geom, "annee": annee}
    if code_cultu:
        params["code_cultu"] = code_cultu

    try:
        r = requests.get("https://apicarto.ign.fr/api/rpg/v2", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return jsonify({"error": f"Impossible de contacter l'API RPG de l'IGN : {e}"}), 502

    features = data.get("features", []) if isinstance(data, dict) else []
    resultats = []
    for f in features:
        props = f.get("properties", {}) or {}
        geom_f = f.get("geometry", {}) or {}
        wkt, geojson_ring = _extraire_geometrie(geom_f)

        resultats.append({
            "id_parcel": props.get("id_parcel") or props.get("ID_PARCEL"),
            "surf_parc_ha": props.get("surf_parc") or props.get("SURF_PARC"),
            "code_cultu": props.get("code_cultu") or props.get("CODE_CULTU"),
            "code_group": props.get("code_group") or props.get("CODE_GROUP"),
            "wkt": wkt,
            "geojson_coords": geojson_ring,  # [[lon,lat], ...], utilise pour dessiner sur la carte
        })

    return jsonify({
        "nb_resultats": len(resultats),
        "resultats": resultats,
        "source": "RPG (IGN/ASP) via apicarto.ign.fr -- Licence Ouverte",
    })


def _extraire_geometrie(geom_f):
    """
    Extrait d'une geometrie GeoJSON (Polygon ou MultiPolygon) l'anneau exterieur le plus
    grand, sous forme de WKT Traccar ET de coordonnees brutes [lon,lat] pour affichage
    carte. Factorise entre la recherche de parcelles et celle d'ilots (meme logique).
    """
    if geom_f.get("type") == "Polygon" and geom_f.get("coordinates"):
        # anneau exterieur uniquement (index 0) -- les eventuels trous internes (index 1+)
        # ne sont pas geres, comme pour la lecture des geofences Traccar existantes
        # (dashboard._parse_geofence_wkt fait la meme simplification)
        ring = geom_f["coordinates"][0]
        return _geojson_polygon_vers_wkt_traccar(ring), ring
    elif geom_f.get("type") == "MultiPolygon" and geom_f.get("coordinates"):
        # ne garde que le plus grand polygone du multi-polygone
        plus_grand = max(geom_f["coordinates"], key=lambda poly: len(poly[0]))
        ring = plus_grand[0]
        return _geojson_polygon_vers_wkt_traccar(ring), ring
    return None, None


@traccar_import_bp.route("/api/rpg_ilots_search")
def api_rpg_ilots_search():
    """
    Recherche les ilots agricoles anonymes du RPG (Registre Parcellaire Graphique) dans une
    zone donnee. Contrairement a /api/rpg_search (qui passe par l'API Carto simplifiee,
    module RPG v2, qui ne renvoie que des PARCELLES pour les millesimes >= 2015), cette
    route interroge DIRECTEMENT le flux WFS de la Geoplateforme IGN sur la couche des ilots
    anonymes -- l'API Carto RPG ne les expose plus depuis l'edition 2022 (classe
    ILOTS_ANONYMES supprimee cote API Carto), mais la couche WFS "RPG.LATEST:ilots_anonymes"
    reste disponible et correspond a la derniere edition du RPG (2024-01-01 au moment de
    l'ecriture -- "LATEST" indique qu'il n'existe PAS de selection par millesime pour cette
    couche, contrairement aux parcelles).

    Interet des ilots par rapport aux parcelles pour une geofence Traccar : l'ilot suit des
    limites physiques stables (route, chemin, ruisseau, haie...) et ne change generalement
    pas d'une campagne a l'autre, contrairement au decoupage en parcelles qui peut se
    redessiner chaque annee selon les cultures implantees.

    Query params : bbox=ouest,sud,est,nord (WGS84) -- recherche uniquement par zone, pas de
    recherche ponctuelle pour cette route (les ilots sont plus grands, une recherche par
    zone est plus adaptee que par point unique).
    """
    bbox_raw = request.args.get("bbox")
    if not bbox_raw:
        return jsonify({"error": "Parametre bbox requis (format : ouest,sud,est,nord)"}), 400
    try:
        ouest, sud, est, nord = [float(v) for v in bbox_raw.split(",")]
    except (TypeError, ValueError):
        return jsonify({"error": "bbox invalide, format attendu : ouest,sud,est,nord"}), 400

    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": "RPG.LATEST:ilots_anonymes",
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": "urn:ogc:def:crs:EPSG::4326",
        "BBOX": f"{sud},{ouest},{nord},{est},urn:ogc:def:crs:EPSG::4326",
        "COUNT": 500,
    }

    try:
        r = requests.get("https://data.geopf.fr/wfs/ows", params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return jsonify({"error": f"Impossible de contacter le flux WFS de la Geoplateforme (ilots RPG) : {e}"}), 502

    features = data.get("features", []) if isinstance(data, dict) else []
    resultats = []
    for f in features:
        props = f.get("properties", {}) or {}
        geom_f = f.get("geometry", {}) or {}
        wkt, geojson_ring = _extraire_geometrie(geom_f)

        # Les noms d'attributs exacts de cette couche n'ont pas ete confirmes de maniere
        # certaine (documentation officielle peu prolixe sur ce point precis) -- on tente
        # plusieurs variantes plausibles (ID_ILOT / id_ilot, SURF_ILOT / surf_ilot) et on
        # renvoie egalement toutes les proprietes brutes, pour ne perdre aucune information
        # meme si les noms exacts different de ce qui est anticipe ici.
        resultats.append({
            "id_ilot": props.get("id_ilot") or props.get("ID_ILOT"),
            "surf_ilot_ha": props.get("surf_ilot") or props.get("SURF_ILOT"),
            "wkt": wkt,
            "geojson_coords": geojson_ring,
            "proprietes_brutes": props,  # filet de securite si les cles ci-dessus ne matchent pas
        })

    return jsonify({
        "nb_resultats": len(resultats),
        "resultats": resultats,
        "source": "RPG Ilots anonymes (IGN/ASP) via flux WFS Geoplateforme -- Licence Ouverte",
    })
