"""
Blueprint de sauvegarde/restauration de la configuration applicative (catalogue produits,
outils, cultures, parcelles, exploitation) au format JSON -- distinct de config.json (qui ne
couvre que Traccar/NDVI/securite) : ceci concerne les DONNEES metier stockees en base.

Extrait de dashboard.py. Depend de DASHBOARD_VERSION (reference via "import dashboard").
"""
import os
import os
import io
import json
import sqlite3
import shutil
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file, session as flask_session

import dashboard

config_backup_bp = Blueprint("config_backup", __name__)


@config_backup_bp.before_request
def _require_login():
    """Meme authentification que le reste de l'application."""
    if not flask_session.get("logged_in"):
        return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401


@config_backup_bp.route("/api/export_config")
def export_config():
    """Exporte toute la configuration (catalogue, outils, cultures, parcelles, exploitation) en JSON."""
    DB_PATH = 'database.db'
    config = {}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee FROM catalog_products")
        config['catalog_products'] = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT keyword, intervention FROM catalog_tools")
        config['catalog_tools'] = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT nom, code_oepp, debut_mmdd, fin_mmdd FROM cultures")
        config['cultures'] = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT geofence_id, identifiant, nom_parcelle, statut, statut_auto FROM parcelles")
        config['parcelles'] = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT siret, raison_sociale, applicateur, certiphyto, materiel, num_controle, date_controle FROM exploitation WHERE id = 1")
        row = cur.fetchone()
        config['exploitation'] = dict(row) if row else {}

    config['_export_date'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    config['_export_version'] = dashboard.DASHBOARD_VERSION

    json_content = json.dumps(config, ensure_ascii=False, indent=2)
    today = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        io.BytesIO(json_content.encode('utf-8')),
        as_attachment=True,
        download_name=f"config_dashboard_{today}.json",
        mimetype='application/json'
    )


def validate_config_structure(config):
    """
    Valide la structure du fichier de configuration avant tout import.
    Retourne (True, "") si valide, (False, message_erreur) sinon.
    Vérifie les types et la présence des clés attendues pour chaque section,
    sans bloquer sur l'absence totale d'une section (un export partiel reste valide).
    """
    if not isinstance(config, dict):
        return False, "Le fichier ne contient pas un objet JSON valide."

    # Au moins une des sections connues doit être présente, sinon ce n'est probablement
    # pas un fichier de configuration de cette application.
    sections_connues = {'catalog_products', 'catalog_tools', 'cultures', 'parcelles', 'exploitation'}
    if not sections_connues.intersection(config.keys()):
        return False, "Aucune section reconnue dans ce fichier (catalog_products, cultures, parcelles…). Est-ce le bon fichier ?"

    if 'catalog_products' in config:
        if not isinstance(config['catalog_products'], list):
            return False, "catalog_products doit être une liste."
        for i, p in enumerate(config['catalog_products']):
            if not isinstance(p, dict):
                return False, f"catalog_products[{i}] doit être un objet."
            if not p.get('name'):
                return False, f"catalog_products[{i}] : le champ 'name' est obligatoire et manquant."
            if not p.get('type'):
                return False, f"catalog_products[{i}] ('{p.get('name')}') : le champ 'type' est obligatoire et manquant."

    if 'catalog_tools' in config:
        if not isinstance(config['catalog_tools'], list):
            return False, "catalog_tools doit être une liste."
        for i, t in enumerate(config['catalog_tools']):
            if not isinstance(t, dict) or not t.get('keyword'):
                return False, f"catalog_tools[{i}] : le champ 'keyword' est obligatoire et manquant."

    if 'cultures' in config:
        if not isinstance(config['cultures'], list):
            return False, "cultures doit être une liste."
        for i, c in enumerate(config['cultures']):
            if not isinstance(c, dict) or not c.get('nom'):
                return False, f"cultures[{i}] : le champ 'nom' est obligatoire et manquant."
            for champ_date in ('debut_mmdd', 'fin_mmdd'):
                val = c.get(champ_date, '01-01')
                if not isinstance(val, str) or len(val) != 5 or val[2] != '-':
                    return False, f"cultures[{i}] ('{c.get('nom')}') : '{champ_date}' doit être au format MM-DD (ex: 09-01)."

    if 'parcelles' in config:
        if not isinstance(config['parcelles'], list):
            return False, "parcelles doit être une liste."
        for i, p in enumerate(config['parcelles']):
            if not isinstance(p, dict) or p.get('geofence_id') is None:
                return False, f"parcelles[{i}] : le champ 'geofence_id' est obligatoire et manquant."
            try:
                int(p['geofence_id'])
            except (ValueError, TypeError):
                return False, f"parcelles[{i}] : 'geofence_id' doit être un nombre entier."

    if 'exploitation' in config and config['exploitation']:
        if not isinstance(config['exploitation'], dict):
            return False, "exploitation doit être un objet."

    return True, ""


@config_backup_bp.route("/api/import_config", methods=["POST"])
def import_config():
    """Importe une configuration depuis un fichier JSON exporté précédemment. Remplace les données existantes."""
    DB_PATH = 'database.db'
    try:
        config = request.get_json(force=True)
    except Exception:
        return jsonify({"status": "error", "message": "JSON invalide"}), 400

    is_valid, error_message = validate_config_structure(config)
    if not is_valid:
        return jsonify({"status": "error", "message": f"Fichier rejeté avant import (aucune donnée modifiée) : {error_message}"}), 400

    # Sauvegarde dédiée juste avant l'import, horodatée à la seconde pour ne jamais
    # être écrasée par la sauvegarde quotidienne automatique ni par un import précédent.
    backup_filename = None
    try:
        import shutil
        if os.path.exists(DB_PATH):
            backup_dir = "backups"
            os.makedirs(backup_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%Hh%Mm%S")
            backup_filename = f"database_avant_import_{timestamp}.db"
            backup_path = os.path.join(backup_dir, backup_filename)
            shutil.copy2(DB_PATH, backup_path)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Échec de la sauvegarde de sécurité, import annulé : {e}"}), 500

    counts = {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()

            if 'catalog_products' in config:
                cur.execute("DELETE FROM catalog_products")
                for p in config['catalog_products']:
                    cur.execute(
                        """INSERT INTO catalog_products
                           (type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (p.get('type'), p.get('name'), p.get('amm', 'N/A'), p.get('dose', 0.0),
                         p.get('unit', ''), p.get('culture', ''), p.get('bbch', ''), p.get('dre', 0),
                         p.get('target', ''), p.get('bio', 0), p.get('dar', 0), p.get('dose_homologuee', 0))
                    )
                counts['catalog_products'] = len(config['catalog_products'])

            if 'catalog_tools' in config:
                cur.execute("DELETE FROM catalog_tools")
                for t in config['catalog_tools']:
                    cur.execute(
                        "INSERT INTO catalog_tools (keyword, intervention) VALUES (?, ?)",
                        (t.get('keyword'), t.get('intervention'))
                    )
                counts['catalog_tools'] = len(config['catalog_tools'])

            if 'cultures' in config:
                cur.execute("DELETE FROM cultures")
                for c in config['cultures']:
                    cur.execute(
                        "INSERT INTO cultures (nom, code_oepp, debut_mmdd, fin_mmdd) VALUES (?, ?, ?, ?)",
                        (c.get('nom'), c.get('code_oepp', ''), c.get('debut_mmdd', '01-01'), c.get('fin_mmdd', '12-31'))
                    )
                counts['cultures'] = len(config['cultures'])

            if 'parcelles' in config:
                cur.execute("DELETE FROM parcelles")
                for p in config['parcelles']:
                    cur.execute(
                        """INSERT INTO parcelles (geofence_id, identifiant, nom_parcelle, statut, statut_auto)
                           VALUES (?, ?, ?, ?, ?)""",
                        (p.get('geofence_id'), p.get('identifiant', ''), p.get('nom_parcelle', ''),
                         p.get('statut', ''), p.get('statut_auto', 1))
                    )
                counts['parcelles'] = len(config['parcelles'])

            if 'exploitation' in config and config['exploitation']:
                e = config['exploitation']
                cur.execute(
                    """UPDATE exploitation SET siret=?, raison_sociale=?, applicateur=?, certiphyto=?,
                       materiel=?, num_controle=?, date_controle=? WHERE id=1""",
                    (e.get('siret', ''), e.get('raison_sociale', ''), e.get('applicateur', ''),
                     e.get('certiphyto', ''), e.get('materiel', ''), e.get('num_controle', ''),
                     e.get('date_controle', ''))
                )
                counts['exploitation'] = 1

            conn.commit()
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Erreur pendant l'import : {e}. Une sauvegarde de la base d'avant import a été conservée : {backup_filename}"
        }), 500

    return jsonify({"status": "success", "counts": counts, "backup_filename": backup_filename})
