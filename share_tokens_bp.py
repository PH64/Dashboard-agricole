"""
Blueprint des liens de partage en lecture seule (page Synthese de campagne consultable sans
compte, avec expiration configurable).

Extrait de dashboard.py (module autonome -- sa propre table SQLite). Contrairement aux
precedents blueprints extraits, celui-ci est importe PAR dashboard.py (pas l'inverse) : le
decorateur "login_or_share_token_required", utilise par plusieurs routes du noyau (Synthese,
phenologie...), appelle _is_valid_share_token() pour verifier un token présent dans l'URL.
Aucun risque d'import circulaire dans ce sens : ce fichier n'importe rien de dashboard.py.
"""
import sqlite3
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session as flask_session

share_tokens_bp = Blueprint("share_tokens", __name__)


@share_tokens_bp.before_request
def _require_login():
    """Meme authentification que le reste de l'application (les routes de CE blueprint
    gerent uniquement la creation/revocation de liens -- la CONSULTATION via token, elle, se
    fait par login_or_share_token_required dans dashboard.py, pas ici)."""
    if not flask_session.get("logged_in"):
        return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401


def _ensure_share_tokens_table():
    with sqlite3.connect('database.db') as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_tokens (
                token TEXT PRIMARY KEY,
                label TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
        """)

def _is_valid_share_token(token):
    _ensure_share_tokens_table()
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT expires_at FROM share_tokens WHERE token = ?", (token,)).fetchone()
        if not row:
            return False
        if row["expires_at"]:
            try:
                if datetime.now() > datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S"):
                    return False
            except Exception:
                pass
        return True

@share_tokens_bp.route("/api/share_tokens", methods=["GET", "POST"])
def api_share_tokens():
    """
    Gère les liens de partage en lecture seule (actuellement : page Synthèse de campagne
    uniquement). POST crée un nouveau lien avec une durée de validité en jours (7 par
    défaut) ; GET liste les liens actifs pour pouvoir les révoquer.
    """
    _ensure_share_tokens_table()
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            label = str(data.get("label", "")).strip() or "Lien de partage"
            duree_jours = int(data.get("duree_jours", 7) or 7)
            token = secrets.token_urlsafe(24)
            expires_at = (datetime.now() + timedelta(days=duree_jours)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO share_tokens (token, label, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, label, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), expires_at)
            )
            conn.commit()
            return jsonify({"token": token, "label": label, "expires_at": expires_at,
                             "url": f"/synthese?token={token}"})

        cur = conn.execute("SELECT * FROM share_tokens ORDER BY created_at DESC")
        return jsonify([dict(r) for r in cur.fetchall()])


@share_tokens_bp.route("/api/share_tokens/<token>", methods=["DELETE"])
def api_share_token_delete(token):
    """Révoque un lien de partage."""
    _ensure_share_tokens_table()
    with sqlite3.connect('database.db') as conn:
        conn.execute("DELETE FROM share_tokens WHERE token = ?", (token,))
        conn.commit()
    return jsonify({"status": "ok"})
