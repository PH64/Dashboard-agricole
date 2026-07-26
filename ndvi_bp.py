"""
Blueprint NDVI : tuiles colorisees (Sentinel Hub / Copernicus Data Space), configuration des
identifiants, et historique NDVI par parcelle/sous-parcelle (capture manuelle, sparkline).

Extrait de dashboard.py (module isole, verifie par analyse de dependances -- aucune autre
partie de l'application ne touche a son etat interne). Depend de 4 elements de dashboard.py,
references via le module lui-meme (pas via 'from dashboard import ...') pour toujours voir
leur valeur a jour, y compris apres un rechargement de configuration Traccar :
  - dashboard.TRACCAR_URL, dashboard.safe_get, dashboard._parse_geofence_wkt,
    dashboard._ensure_sous_parcelles_table (utilises uniquement dans api_ndvi_capture, pour
    retrouver la geometrie d'une parcelle sans sous-parcelle associee).
"""
import os
import io
import json
import time
import sqlite3
import threading
from datetime import datetime, timedelta

import requests
from flask import Blueprint, request, jsonify, send_file, session as flask_session

import dashboard

ndvi_bp = Blueprint("ndvi", __name__)


@ndvi_bp.before_request
def _require_login():
    """Meme authentification que le reste de l'application (voir interventions.py pour le
    meme motif) -- une seule verification pour toutes les routes de ce blueprint plutot
    qu'un decorateur @login_required repete sur chaque route."""
    if not flask_session.get("logged_in"):
        return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401


# ================= NDVI COLORISE (Sentinel Hub / Copernicus Data Space) =================
# Pour afficher un vrai NDVI colore (et pas juste l'imagerie satellite "vraies couleurs"),
# il faut calculer (NIR-Rouge)/(NIR+Rouge) a partir des bandes Sentinel-2. Ce calcul est fait
# par une API "Process", qui necessite un compte gratuit et des identifiants OAuth.
#
# ATTENTION : il existe DEUX fournisseurs possibles, avec des serveurs differents. Utiliser
# le mauvais serveur avec des identifiants valides provoque une erreur 401 :
#   - "cdse" = Copernicus Data Space Ecosystem (https://dataspace.copernicus.eu) -- RECOMMANDE,
#     c'est la plateforme gratuite actuelle de l'Agence Spatiale Europeenne.
#   - "shub" = Sentinel Hub classique (https://www.sentinel-hub.com) -- ancien compte payant/trial.
#
# Mise en place (une seule fois), via la boite de dialogue "⚙️ Configurer NDVI" de l'appli,
# ou directement dans config.json :
#      {
#        "sentinelhub": {
#          "provider": "cdse",
#          "client_id": "xxxx",
#          "client_secret": "xxxx"
#        }
#      }

_SH_PROVIDERS = {
    "cdse": {
        "label": "Copernicus Data Space Ecosystem",
        "token_url": "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        "process_url": "https://sh.dataspace.copernicus.eu/api/v1/process",
        "statistics_url": "https://sh.dataspace.copernicus.eu/api/v1/statistics",
    },
    "shub": {
        "label": "Sentinel Hub (compte classique)",
        "token_url": "https://services.sentinel-hub.com/oauth/token",
        "process_url": "https://services.sentinel-hub.com/api/v1/process",
        "statistics_url": "https://services.sentinel-hub.com/api/v1/statistics",
    },
}

def _load_sentinelhub_config():
    defaults = {
        "provider": "cdse",
        "client_id": "",
        "client_secret": "",
        "tile_cache_seconds": 3600,  # les tuiles NDVI changent peu, on peut cacher longtemps
    }
    try:
        if os.path.exists("config.json"):
            with open("config.json", "r") as f:
                data = json.load(f)
                defaults.update(data.get("sentinelhub", {}))
    except Exception:
        pass
    if defaults.get("provider") not in _SH_PROVIDERS:
        defaults["provider"] = "cdse"
    return defaults

_shcfg = _load_sentinelhub_config()
SH_PROVIDER      = _shcfg["provider"]
SH_CLIENT_ID     = _shcfg["client_id"]
SH_CLIENT_SECRET = _shcfg["client_secret"]
SH_TILE_CACHE_S  = _shcfg.get("tile_cache_seconds", 3600)

_sh_token = None
_sh_token_expiry = 0
_sh_last_error = None
_sh_tile_cache = {}   # {(z,x,y): (bytes_png, timestamp)}
_sh_lock = threading.Lock()

# 1x1 pixel PNG transparent, renvoye quand les identifiants ne sont pas configures
_TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100e2216bc4000000"
    "0049454e44ae426082"
)

def _sh_get_token(provider=None, client_id=None, client_secret=None):
    """
    Recupere (et met en cache) un token OAuth. Sans arguments, utilise la config globale
    (SH_PROVIDER/SH_CLIENT_ID/SH_CLIENT_SECRET). Avec arguments, sert au test de connexion
    depuis la boite de dialogue (sans toucher au cache global).
    """
    global _sh_token, _sh_token_expiry, _sh_last_error

    use_global = provider is None
    prov   = provider or SH_PROVIDER
    cid    = client_id or SH_CLIENT_ID
    secret = client_secret or SH_CLIENT_SECRET

    now = time.time()
    if use_global and _sh_token and now < _sh_token_expiry - 60:
        return _sh_token, None
    if not cid or not secret:
        return None, "Aucun identifiant configure"

    token_url = _SH_PROVIDERS.get(prov, _SH_PROVIDERS["cdse"])["token_url"]
    try:
        r = requests.post(
            token_url,
            data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
            timeout=10,
        )
        r.raise_for_status()
        token_data = r.json()
        token = token_data["access_token"]
        if use_global:
            _sh_token = token
            _sh_token_expiry = now + token_data.get("expires_in", 3600)
            _sh_last_error = None
        return token, None
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        detail = ""
        try:
            detail = e.response.json().get("error_description", "")
        except Exception:
            detail = (e.response.text[:200] if e.response is not None else str(e))
        err = f"Authentification refusee ({status}) sur {token_url} : {detail}"
        if use_global:
            _sh_last_error = err
        app.logger.warning("Erreur token NDVI [%s] : %s", prov, err)
        return None, err
    except Exception as e:
        err = f"Erreur de connexion a {token_url} : {e}"
        if use_global:
            _sh_last_error = err
        app.logger.exception("Erreur recuperation token NDVI")
        return None, err

def _tile_to_bbox_3857(z, x, y):
    """Convertit des coordonnees de tuile slippy-map (z/x/y, standard OSM/Leaflet) en bbox EPSG:3857."""
    import math
    n = 2.0 ** z

    def lonlat_at(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ty / n)))
        lat = math.degrees(lat_rad)
        return lon, lat

    def to_3857(lon, lat):
        mx = lon * 20037508.342789244 / 180.0
        my = math.log(math.tan((90 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
        my = my * 20037508.342789244 / 180.0
        return mx, my

    lon_min, lat_max = lonlat_at(x, y)
    lon_max, lat_min = lonlat_at(x + 1, y + 1)
    x_min, y_min = to_3857(lon_min, lat_min)
    x_max, y_max = to_3857(lon_max, lat_max)
    return [x_min, y_min, x_max, y_max]

# Evalscript : calcule le NDVI et le colorise (rouge = sol nu/stress, vert = vegetation dense)
_NDVI_EVALSCRIPT = """
//VERSION=3
function setup() {
  return { input: ["B04", "B08", "dataMask"], output: { bands: 4 } };
}
const ramp = [
  [-1.0, [0x8b,0x00,0x00]],
  [0.0,  [0xff,0x45,0x00]],
  [0.2,  [0xff,0xa5,0x00]],
  [0.3,  [0xff,0xff,0x00]],
  [0.4,  [0x9a,0xcd,0x32]],
  [0.6,  [0x22,0x8b,0x22]],
  [1.0,  [0x00,0x64,0x00]]
];
function lerp(a, b, t) { return a + (b - a) * t; }
function colorize(ndvi) {
  for (let i = 0; i < ramp.length - 1; i++) {
    const v0 = ramp[i][0], c0 = ramp[i][1];
    const v1 = ramp[i+1][0], c1 = ramp[i+1][1];
    if (ndvi >= v0 && ndvi <= v1) {
      const t = (ndvi - v0) / (v1 - v0);
      return [lerp(c0[0],c1[0],t)/255, lerp(c0[1],c1[1],t)/255, lerp(c0[2],c1[2],t)/255];
    }
  }
  const edge = ndvi < ramp[0][0] ? ramp[0][1] : ramp[ramp.length-1][1];
  return [edge[0]/255, edge[1]/255, edge[2]/255];
}
function evaluatePixel(s) {
  const ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 1e-6);
  const [r, g, b] = colorize(ndvi);
  return [r, g, b, s.dataMask];
}
"""

@ndvi_bp.route("/api/ndvi_tile/<int:z>/<int:x>/<int:y>.png")
def api_ndvi_tile(z, x, y):
    """
    Proxy tuile NDVI coloree (Sentinel-2, image la moins nuageuse autour de la date demandee).
    Parametre optionnel 'date' (YYYY-MM-DD) : permet de consulter l'historique NDVI a une date
    passee plutot que seulement les 30 derniers jours -- la recherche porte alors sur les 30
    jours precedant cette date (fenetre coherente avec la frequence de repassage Sentinel-2).
    """
    ref_date_str = request.args.get("date", "")
    cache_key = (z, x, y, ref_date_str)
    now = time.time()
    with _sh_lock:
        cached = _sh_tile_cache.get(cache_key)
        if cached and (now - cached[1] < SH_TILE_CACHE_S):
            return send_file(io.BytesIO(cached[0]), mimetype="image/png")

    if not SH_CLIENT_ID or not SH_CLIENT_SECRET:
        # Pas d'identifiants du tout : cas normal (NDVI pas encore configure), tuile transparente
        return send_file(io.BytesIO(_TRANSPARENT_PNG), mimetype="image/png")

    token, err = _sh_get_token()
    if not token:
        # Identifiants presents mais refuses : vraie erreur d'authentification -> 401 explicite
        return jsonify({"error": err or "Authentification NDVI echouee"}), 401

    bbox = _tile_to_bbox_3857(z, x, y)
    if ref_date_str:
        try:
            end = datetime.strptime(ref_date_str, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            end = datetime.utcnow()
    else:
        end = datetime.utcnow()
    start = end - timedelta(days=30)

    payload = {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/3857"}
            },
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {
                        "from": start.strftime("%Y-%m-%dT00:00:00Z"),
                        "to":   end.strftime("%Y-%m-%dT23:59:59Z")
                    },
                    "mosaickingOrder": "leastCC"
                }
            }]
        },
        "output": {
            "width": 256, "height": 256,
            "responses": [{"identifier": "default", "format": {"type": "image/png"}}]
        },
        "evalscript": _NDVI_EVALSCRIPT
    }

    process_url = _SH_PROVIDERS.get(SH_PROVIDER, _SH_PROVIDERS["cdse"])["process_url"]
    try:
        r = requests.post(
            process_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        if r.status_code in (401, 403):
            detail = ""
            try: detail = r.json().get("error", {}).get("message", "")
            except Exception: detail = r.text[:200]
            app.logger.warning("NDVI process API refusee (%s) : %s", r.status_code, detail)
            return jsonify({"error": f"Sentinel Hub a refuse la requete ({r.status_code}) : {detail}"}), r.status_code
        r.raise_for_status()
        img_bytes = r.content
        with _sh_lock:
            _sh_tile_cache[cache_key] = (img_bytes, now)
            if len(_sh_tile_cache) > 5000:
                _sh_tile_cache.clear()
        return send_file(io.BytesIO(img_bytes), mimetype="image/png")
    except Exception:
        app.logger.exception("Erreur tuile NDVI (z=%s x=%s y=%s)", z, x, y)
        return send_file(io.BytesIO(_TRANSPARENT_PNG), mimetype="image/png")


def _save_config_section(section, data):
    """Fusionne et sauvegarde une section dans config.json (preserve les autres sections)."""
    cfg = {}
    try:
        if os.path.exists("config.json"):
            with open("config.json", "r") as f:
                cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault(section, {})
    cfg[section].update(data)
    with open("config.json", "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg[section]


@ndvi_bp.route("/api/ndvi_status")
def api_ndvi_status():
    """Etat de la connexion NDVI (pour affichage et pré-remplissage dans la boite de dialogue)."""
    configured = bool(SH_CLIENT_ID and SH_CLIENT_SECRET)
    masked = ""
    if SH_CLIENT_ID:
        masked = (SH_CLIENT_ID[:4] + "…" + SH_CLIENT_ID[-4:]) if len(SH_CLIENT_ID) > 8 else (SH_CLIENT_ID[:2] + "…")
    return jsonify({
        "configured": configured,
        "client_id_masked": masked,
        # Valeurs completes renvoyees pour pre-remplir le formulaire de configuration : sans
        # cela, l'utilisateur doit reproduire son Client ID/Secret Sentinel Hub a chaque fois
        # qu'il rouvre la boite de dialogue, alors qu'ils sont deja enregistres et actifs
        # cote serveur. Meme niveau de confiance que la config Traccar (voir /api/traccar_import_config) :
        # cette route est deja protegee par la meme authentification que le reste du dashboard.
        "client_id": SH_CLIENT_ID,
        "client_secret": SH_CLIENT_SECRET,
        "provider": SH_PROVIDER,
        "provider_label": _SH_PROVIDERS.get(SH_PROVIDER, _SH_PROVIDERS["cdse"])["label"],
        "last_error": _sh_last_error,
    })


@ndvi_bp.route("/api/ndvi_config", methods=["POST"])
def api_ndvi_config():
    """Enregistre les identifiants NDVI saisis depuis la boite de dialogue, apres avoir verifie qu'ils fonctionnent."""
    global SH_PROVIDER, SH_CLIENT_ID, SH_CLIENT_SECRET, _sh_token, _sh_token_expiry, _sh_tile_cache, _sh_last_error

    data = request.get_json(silent=True) or {}
    provider      = str(data.get("provider", "cdse")).strip()
    client_id     = str(data.get("client_id", "")).strip()
    client_secret = str(data.get("client_secret", "")).strip()
    if provider not in _SH_PROVIDERS:
        provider = "cdse"
    if not client_id or not client_secret:
        return jsonify({"error": "Client ID et Client Secret requis"}), 400

    token, err = _sh_get_token(provider=provider, client_id=client_id, client_secret=client_secret)
    if not token:
        return jsonify({"error": err or "Connexion refusee"}), 400

    _save_config_section("sentinelhub", {
        "provider": provider, "client_id": client_id, "client_secret": client_secret
    })
    SH_PROVIDER = provider
    SH_CLIENT_ID = client_id
    SH_CLIENT_SECRET = client_secret
    with _sh_lock:
        _sh_token = token
        _sh_token_expiry = time.time() + 3600
        _sh_tile_cache.clear()
        _sh_last_error = None

    return jsonify({"success": True, "provider_label": _SH_PROVIDERS[provider]["label"]})


# ================= HISTORIQUE NDVI PAR PARCELLE =================
def _ensure_ndvi_history_table():
    with sqlite3.connect('database.db') as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ndvi_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geofence_id TEXT NOT NULL,
                sous_parcelle_id INTEGER DEFAULT NULL,
                date TEXT NOT NULL,
                ndvi_moyen REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def _sh_polygon_ndvi_mean(coords, date_str):
    """
    Calcule le NDVI moyen sur un polygone (liste de [lat, lon]) à une date donnée, via
    l'API Statistics de Sentinel Hub / Copernicus Data Space Ecosystem. Cherche jusqu'à
    10 jours en arrière la dernière image exploitable (nuages/absence de passage peuvent
    laisser plusieurs jours sans donnée), et retourne le point le plus RÉCENT disponible
    dans cette fenêtre, pas nécessairement le jour exact demandé.
    Retourne (ndvi_moyen, date_reelle) ou (None, erreur_message).
    """
    token, err = _sh_get_token()
    if not token:
        return None, err or "Identifiants NDVI non configurés"

    statistics_url = _SH_PROVIDERS.get(SH_PROVIDER, _SH_PROVIDERS["cdse"])["statistics_url"]
    try:
        target_dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        target_dt = datetime.now()
    start_dt = target_dt - timedelta(days=10)

    # GeoJSON attend [lon, lat], notre convention interne est [lat, lon] -- inversion ici.
    geojson_coords = [[lon, lat] for lat, lon in coords]
    if geojson_coords and geojson_coords[0] != geojson_coords[-1]:
        geojson_coords.append(geojson_coords[0])

    evalscript = """
        //VERSION=3
        function setup() {
            return {
                input: [{bands: ["B04", "B08", "dataMask"]}],
                output: [
                    {id: "ndvi", bands: 1, sampleType: "FLOAT32"},
                    {id: "dataMask", bands: 1}
                ]
            };
        }
        function evaluatePixel(sample) {
            let ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04 + 0.0001);
            return { ndvi: [ndvi], dataMask: [sample.dataMask] };
        }
    """
    body = {
        "input": {
            "bounds": {
                "geometry": {"type": "Polygon", "coordinates": [geojson_coords]},
                "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
            },
            "data": [{"type": "sentinel-2-l2a"}],
        },
        "aggregation": {
            "timeRange": {
                "from": start_dt.strftime("%Y-%m-%dT00:00:00Z"),
                "to": target_dt.strftime("%Y-%m-%dT23:59:59Z"),
            },
            "aggregationInterval": {"of": "P1D"},
            "evalscript": evalscript,
            "resx": 10, "resy": 10,
        },
        "calculations": {"default": {}},
    }

    try:
        r = requests.post(
            statistics_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        return None, f"Erreur API Statistics : {e}"

    # Cherche le jour le plus RÉCENT avec des pixels valides (dataMask non nul), en partant
    # de la fin (jours les plus proches de la date demandée en premier).
    for interval in reversed(data):
        try:
            outputs = interval.get("outputs", {})
            ndvi_stats = outputs.get("ndvi", {}).get("bands", {}).get("B0", {}).get("stats", {})
            if ndvi_stats.get("sampleCount", 0) > 0 and ndvi_stats.get("mean") is not None:
                interval_date = interval.get("interval", {}).get("from", "")[:10]
                return round(ndvi_stats["mean"], 4), interval_date
        except Exception:
            continue

    return None, "Aucune image exploitable trouvée sur les 10 derniers jours (nuages ?)"


@ndvi_bp.route("/api/ndvi_capture", methods=["POST"])
def api_ndvi_capture():
    """
    Calcule le NDVI moyen d'une parcelle (ou sous-parcelle) et l'enregistre dans
    l'historique. Déclenché manuellement depuis la carte de chantier en mode NDVI, plutôt
    qu'automatiquement en tâche de fond -- l'utilisateur choisit le bon moment (image
    dégagée visible à l'écran) plutôt que de capturer une image potentiellement nuageuse
    sans le savoir.
    """
    _ensure_ndvi_history_table()
    import math
    data = request.get_json(silent=True) or {}
    geofence_id = data.get("geofence_id")
    sous_parcelle_id = data.get("sous_parcelle_id")
    date_str = data.get("date") or datetime.now().strftime("%Y-%m-%d")

    if not geofence_id:
        return jsonify({"error": "geofence_id requis"}), 400

    coords = None
    if sous_parcelle_id:
        try:
            dashboard._ensure_sous_parcelles_table()
            with sqlite3.connect('database.db') as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT polygon FROM sous_parcelles WHERE id = ?", (sous_parcelle_id,)).fetchone()
                if row:
                    coords = json.loads(row["polygon"])
        except Exception:
            coords = None

    if not coords:
        try:
            raw_geofences_list = dashboard.safe_get(f"{dashboard.TRACCAR_URL}/geofences")
            if isinstance(raw_geofences_list, list):
                for g in raw_geofences_list:
                    if str(g.get("id")) == str(geofence_id):
                        geom = dashboard._parse_geofence_wkt(g.get("area"))
                        if geom and geom["type"] == "polygon":
                            coords = geom["coords"]
                        elif geom and geom["type"] == "circle":
                            # Approxime le cercle par un octogone pour l'appel Statistics API
                            lat0, lon0 = geom["center"]
                            r_deg = geom["radius"] / 111320.0
                            coords = [[lat0 + r_deg*math.sin(a), lon0 + r_deg*math.cos(a)] for a in [i*math.pi/4 for i in range(8)]]
                        break
        except Exception:
            coords = None

    if not coords or len(coords) < 3:
        return jsonify({"error": "Géométrie de la parcelle introuvable"}), 404

    ndvi_moyen, resultat = _sh_polygon_ndvi_mean(coords, date_str)
    if ndvi_moyen is None:
        return jsonify({"error": resultat}), 502

    date_reelle = resultat
    with sqlite3.connect('database.db') as conn:
        # Évite les doublons : un seul point par jour et par parcelle/sous-parcelle (on
        # remplace si un point existe déjà pour cette date réelle).
        conn.execute(
            "DELETE FROM ndvi_history WHERE geofence_id = ? AND sous_parcelle_id IS ? AND date = ?",
            (str(geofence_id), sous_parcelle_id, date_reelle)
        )
        conn.execute(
            "INSERT INTO ndvi_history (geofence_id, sous_parcelle_id, date, ndvi_moyen, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(geofence_id), sous_parcelle_id, date_reelle, ndvi_moyen, datetime.now().isoformat())
        )
        conn.commit()

    return jsonify({"ndvi_moyen": ndvi_moyen, "date": date_reelle})


@ndvi_bp.route("/api/ndvi_history")
def api_ndvi_history():
    """Historique des points NDVI enregistrés pour une parcelle (ou sous-parcelle)."""
    _ensure_ndvi_history_table()
    geofence_id = request.args.get("geofence_id")
    sous_parcelle_id = request.args.get("sous_parcelle_id")
    if not geofence_id:
        return jsonify({"error": "geofence_id requis"}), 400

    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        if sous_parcelle_id:
            rows = conn.execute(
                "SELECT date, ndvi_moyen FROM ndvi_history WHERE geofence_id = ? AND sous_parcelle_id = ? ORDER BY date ASC",
                (str(geofence_id), sous_parcelle_id)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, ndvi_moyen FROM ndvi_history WHERE geofence_id = ? AND sous_parcelle_id IS NULL ORDER BY date ASC",
                (str(geofence_id),)
            ).fetchall()

    return jsonify([dict(r) for r in rows])

