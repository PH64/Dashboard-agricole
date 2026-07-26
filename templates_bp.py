"""
Blueprint des modeles d'intervention reutilisables : un nom + un type d'intervention + une
liste de produits/doses habituels, pour resaisir un traitement type en 2 clics plutot que de
retaper tous les produits a chaque fois (utilise depuis les formulaires de saisie GPS et
manuelle dans index.html).

Extrait de dashboard.py (module totalement autonome -- sa propre table SQLite, aucune autre
partie de l'application ne l'utilise).
"""
import json
import sqlite3
from datetime import datetime

from flask import Blueprint, request, jsonify, session as flask_session

templates_bp = Blueprint("templates", __name__)


@templates_bp.before_request
def _require_login():
    """Meme authentification que le reste de l'application."""
    if not flask_session.get("logged_in"):
        return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401


def _ensure_intervention_templates_table():
    with sqlite3.connect('database.db') as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intervention_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                intervention_type TEXT NOT NULL,
                products TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

@templates_bp.route("/api/intervention_templates", methods=["GET", "POST"])
def api_intervention_templates():
    """
    Modèles d'intervention réutilisables : un nom + un type d'intervention + une liste de
    produits/doses habituels, pour resaisir un traitement type en 2 clics plutôt que de
    retaper tous les produits à chaque fois.
    """
    _ensure_intervention_templates_table()
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            name = str(data.get("name", "")).strip()
            intervention_type = str(data.get("intervention_type", "")).strip()
            products = data.get("products", [])
            if not name or not intervention_type or not products:
                return jsonify({"error": "Nom, type d'intervention et au moins un produit requis"}), 400
            cur = conn.execute(
                "INSERT INTO intervention_templates (name, intervention_type, products, created_at) VALUES (?, ?, ?, ?)",
                (name, intervention_type, json.dumps(products, ensure_ascii=False), datetime.utcnow().isoformat())
            )
            conn.commit()
            return jsonify({"id": cur.lastrowid, "status": "ok"})

        cur = conn.execute("SELECT * FROM intervention_templates ORDER BY name COLLATE NOCASE")
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            try:
                row["products"] = json.loads(row["products"])
            except Exception:
                row["products"] = []
            rows.append(row)
        return jsonify(rows)


@templates_bp.route("/api/intervention_templates/<int:template_id>", methods=["DELETE"])
def api_intervention_template_delete(template_id):
    """Supprime un modèle d'intervention."""
    _ensure_intervention_templates_table()
    with sqlite3.connect('database.db') as conn:
        conn.execute("DELETE FROM intervention_templates WHERE id = ?", (template_id,))
        conn.commit()
    return jsonify({"status": "ok"})
