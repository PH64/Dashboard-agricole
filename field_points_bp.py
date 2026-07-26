"""
Blueprint des points d'observation terrain geolocalises (adventices, maladie/ravageur, panne,
zone humide...), saisis directement depuis la carte de chantier -- independants de la date
chargee, ce sont des notes de terrain durables.

Extrait de dashboard.py. Contient une copie de _to_float() (conversion tolerante en nombre
decimal, gere virgule et unite collee) plutot qu'un import de dashboard.py : fonction pure de
15 lignes, partagee avec d'autres parties de l'application qui restent dans le noyau -- la
dupliquer evite d'introduire une dependance croisee pour si peu.

Seule veritable dependance a dashboard.py : l'envoi d'une alerte email automatique
(dashboard.envoyer_email / dashboard._charger_config_smtp) quand un point de categorie
"maladie" ou "panne" est cree, si ce type d'alerte est active dans la configuration SMTP
(voir Etat systeme). Reutilise l'infrastructure SMTP commune plutot que d'en dupliquer une.
"""
import re
import sqlite3
from datetime import datetime

from flask import Blueprint, request, jsonify, session as flask_session

import dashboard

field_points_bp = Blueprint("field_points", __name__)


@field_points_bp.before_request
def _require_login():
    """Meme authentification que le reste de l'application."""
    if not flask_session.get("logged_in"):
        return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401


def _to_float(val, default=0.0):
    """Convertit en float, tolère virgule décimale et unité collée (ex: '4,5 m', '3.2m')."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    s = str(val).strip().lower().replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if m:
        try:
            return float(m.group())
        except ValueError:
            return default
    return default


# ================= POINTS D'OBSERVATION TERRAIN (carte de chantier) =================
FIELD_POINT_CATEGORIES = {
    "adventices":  {"label": "Adventices",         "emoji": "⚠️", "color": "#eab308"},
    "maladie":     {"label": "Maladie/Ravageur",   "emoji": "🐛", "color": "#dc2626"},
    "panne":       {"label": "Panne",              "emoji": "🔧", "color": "#64748b"},
    "humide":      {"label": "Zone humide",        "emoji": "💧", "color": "#0ea5e9"},
    "autre":       {"label": "Autre",              "emoji": "📝", "color": "#a855f7"},
}

# ================= SOUS-ZONES (scission d'une parcelle en plusieurs cultures) =================
# Une parcelle Traccar (geofence_id) reste l'unique référence pour tout l'historique existant
# (interventions, carte de chantier, GPS...) : on ne redessine JAMAIS la géofence elle-même.
# Une "sous-parcelle" est une donnée propre à l'application, superposée à une parcelle pour une
# campagne donnée, quand celle-ci est scindée en plusieurs cultures la même année. Sans
# sous-parcelle définie, tout se comporte exactement comme avant (comportement par défaut inchangé).
def _ensure_sous_parcelles_table():
    with sqlite3.connect('database.db') as conn:
        # Migration de compatibilité : la fonctionnalité s'appelait "sous-zone" à sa création
        # et a été renommée en "sous-parcelle" -- si l'ancienne table/colonne existe encore
        # (installation mise à jour depuis une version antérieure), on les renomme plutôt que
        # d'en recréer de nouvelles vides, pour ne perdre aucune donnée déjà saisie.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'sous_zones' in tables and 'sous_parcelles' not in tables:
            conn.execute("ALTER TABLE sous_zones RENAME TO sous_parcelles")
        cols_interv = {row[1] for row in conn.execute("PRAGMA table_info(interventions)").fetchall()}
        if 'sous_zone_id' in cols_interv and 'sous_parcelle_id' not in cols_interv:
            conn.execute("ALTER TABLE interventions RENAME COLUMN sous_zone_id TO sous_parcelle_id")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sous_parcelles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geofence_id TEXT NOT NULL,
                nom TEXT NOT NULL,
                campagne TEXT,
                culture TEXT,
                statut TEXT DEFAULT 'attente',
                surface_ha REAL,
                polygon TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # Colonne de rattachement optionnel sur les interventions (NULL = pas de sous-parcelle,
        # comportement historique inchangé).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(interventions)").fetchall()}
        if 'sous_parcelle_id' not in cols:
            conn.execute("ALTER TABLE interventions ADD COLUMN sous_parcelle_id INTEGER DEFAULT NULL")


def _get_sous_parcelles_info(conn, geofence_id=None):
    """
    Renvoie {sous_parcelle_id: {id, geofence_id, nom, culture, campagne, surface_ha,
    polygon}} pour toutes les sous-parcelles (ou seulement celles d'une géofence donnée si
    geofence_id est précisé). Centralise cette requête : elle était dupliquée par le passé
    dans 7 endroits différents du code (Bilan, Fertilisation, export PDF, Cahier de
    traçabilité, anomalies, phénologie maïs/céréales, Analytique), chacun avec un jeu de
    colonnes parfois incomplet selon l'endroit -- un vrai risque d'incohérence à chaque
    nouvelle fonctionnalité qui y touchait sans se souvenir de reproduire exactement la
    bonne requête partout. N'appelle PAS _ensure_sous_parcelles_table() elle-même : à faire
    par l'appelant, qui sait s'il vient de créer la connexion ou pas.
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if geofence_id is not None:
        cur.execute(
            "SELECT id, geofence_id, nom, culture, campagne, surface_ha, polygon FROM sous_parcelles WHERE geofence_id = ?",
            (str(geofence_id),)
        )
    else:
        cur.execute("SELECT id, geofence_id, nom, culture, campagne, surface_ha, polygon FROM sous_parcelles")
    return {r["id"]: dict(r) for r in cur.fetchall()}


# api_sous_parcelles / api_sous_parcelle_detail : code mort retire (confirme par test
# empirique) -- la version reellement active est celle du blueprint interventions_bp,
# enregistree plus tot et donc prioritaire pour ces memes routes.

def _ensure_field_points_table():
    with sqlite3.connect('database.db') as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS field_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                date TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'autre',
                note TEXT,
                geofence_id TEXT,
                created_at TEXT NOT NULL
            )
        """)

def _alerter_si_point_sensible(category, note, date_str, geofence_id):
    """
    Envoie une alerte email si un point de categorie "maladie" (maladie/ravageur) ou
    "panne" vient d'etre cree, et que ce type d'alerte est active dans la configuration
    SMTP (voir Etat systeme). Echec silencieux (log seulement) : une alerte email qui ne
    part pas ne doit jamais empecher l'enregistrement du point lui-meme, deja fait avant
    cet appel.
    """
    if category not in ("maladie", "panne"):
        return
    try:
        smtp_cfg = dashboard._charger_config_smtp()
        if not smtp_cfg.get("alerte_points_terrain"):
            return
        destinataire = smtp_cfg.get("destinataire_alertes")
        label = FIELD_POINT_CATEGORIES.get(category, {}).get("label", category)
        emoji = FIELD_POINT_CATEGORIES.get(category, {}).get("emoji", "")
        parcelle_txt = f" (parcelle {geofence_id})" if geofence_id else ""
        corps = (
            f"Un nouveau point d'observation a ete signale sur le Dashboard Agricole :\n\n"
            f"{emoji} {label}{parcelle_txt}\n"
            f"Date : {date_str}\n"
            f"Note : {note or '(aucune note)'}"
        )
        dashboard.envoyer_email(destinataire, f"{emoji} {label} signalé sur le terrain", corps)
    except Exception:
        pass  # ne jamais faire echouer la creation du point a cause d'un souci d'alerte


@field_points_bp.route("/api/field_points", methods=["GET", "POST"])
def api_field_points():
    """
    Points d'observation terrain géolocalisés (adventices, maladie, panne, zone humide...),
    saisis directement depuis la carte de chantier. GET renvoie tous les points (indépendants
    de la date chargée, ce sont des notes de terrain durables) ; POST en crée un nouveau.
    """
    _ensure_field_points_table()
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            lat = _to_float(data.get("lat"))
            lon = _to_float(data.get("lon"))
            category = str(data.get("category", "autre")).strip()
            if category not in FIELD_POINT_CATEGORIES:
                category = "autre"
            date_str = str(data.get("date") or datetime.utcnow().strftime("%Y-%m-%d"))
            note = str(data.get("note", "")).strip()
            geofence_id = data.get("geofence_id")
            cur = conn.execute(
                "INSERT INTO field_points (lat, lon, date, category, note, geofence_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lat, lon, date_str, category, note, str(geofence_id) if geofence_id else None,
                 datetime.utcnow().isoformat())
            )
            conn.commit()
            _alerter_si_point_sensible(category, note, date_str, geofence_id)
            return jsonify({"id": cur.lastrowid, "status": "ok"})
        cur = conn.execute("SELECT * FROM field_points ORDER BY id DESC")
        return jsonify([dict(r) for r in cur.fetchall()])


@field_points_bp.route("/api/field_points/<int:point_id>", methods=["PUT", "DELETE"])
def api_field_point_detail(point_id):
    """Modifie ou supprime un point d'observation terrain existant."""
    _ensure_field_points_table()
    with sqlite3.connect('database.db') as conn:
        if request.method == "DELETE":
            conn.execute("DELETE FROM field_points WHERE id = ?", (point_id,))
            conn.commit()
            return jsonify({"status": "ok"})

        data = request.get_json(silent=True) or {}
        category = str(data.get("category", "autre")).strip()
        if category not in FIELD_POINT_CATEGORIES:
            category = "autre"
        note = str(data.get("note", "")).strip()
        date_str = data.get("date")
        if date_str:
            conn.execute(
                "UPDATE field_points SET category = ?, note = ?, date = ? WHERE id = ?",
                (category, note, date_str, point_id)
            )
        else:
            conn.execute(
                "UPDATE field_points SET category = ?, note = ? WHERE id = ?",
                (category, note, point_id)
            )
        conn.commit()
        return jsonify({"status": "ok"})
