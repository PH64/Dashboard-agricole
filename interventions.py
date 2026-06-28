import sqlite3
import json
from datetime import datetime
from flask import Blueprint, request, jsonify

interventions_bp = Blueprint('interventions', __name__)
DB_PATH = 'database.db'

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # Table interventions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interventions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                geofence_id INTEGER NOT NULL,
                exit_time TEXT NOT NULL,
                vehicle_name TEXT,
                tool_detected TEXT,
                intervention_type TEXT,
                products TEXT,
                applied_area REAL,
                UNIQUE(device_id, geofence_id, exit_time)
            )
        ''')

        # Table catalogue produits
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                amm TEXT DEFAULT 'N/A',
                dose REAL DEFAULT 0.0
            )
        ''')

        # Table catalogue outils
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                intervention TEXT NOT NULL
            )
        ''')

        # Pré-remplissage des outils par défaut si vide
        cursor.execute("SELECT COUNT(*) FROM catalog_tools")
        if cursor.fetchone()[0] == 0:
            default_tools = [
                ("RB47", "Labour"), ("LANSAMAN", "Hersage"), ("NG", "Semis"),
                ("UF1201", "Pulvérisation"), ("ZA", "Épandage"), ("XS32", "Déchaumage"),
                ("TIGRE", "Broyage"), ("CHARRUE", "Labour"), ("MOISS", "Récolte")
            ]
            cursor.executemany(
                "INSERT INTO catalog_tools (keyword, intervention) VALUES (?, ?)",
                default_tools
            )

        # Table correspondance parcelles (geofence_id -> identifiant_parcelle)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS parcelles (
                geofence_id INTEGER PRIMARY KEY,
                identifiant TEXT NOT NULL DEFAULT '',
                nom_parcelle TEXT DEFAULT '',
                statut TEXT DEFAULT ''
            )
        ''')

        # Migration colonne statut dans parcelles
        cursor.execute("PRAGMA table_info(parcelles)")
        parc_cols = {row[1] for row in cursor.fetchall()}
        if 'statut' not in parc_cols:
            cursor.execute("ALTER TABLE parcelles ADD COLUMN statut TEXT DEFAULT ''")
        if 'statut_auto' not in parc_cols:
            cursor.execute("ALTER TABLE parcelles ADD COLUMN statut_auto INTEGER DEFAULT 1")
        if 'surface_ha' not in parc_cols:
            cursor.execute("ALTER TABLE parcelles ADD COLUMN surface_ha REAL DEFAULT NULL")

        # Table notes libres par parcelle
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notes_parcelles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geofence_id INTEGER NOT NULL,
                date_note TEXT NOT NULL,
                contenu TEXT NOT NULL
            )
        ''')

        # Table exploitation (SIRET, raison sociale, etc.)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS exploitation (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                siret TEXT DEFAULT '',
                raison_sociale TEXT DEFAULT ''
            )
        ''')
        # Initialiser avec une ligne unique si vide
        cursor.execute("INSERT OR IGNORE INTO exploitation (id, siret, raison_sociale) VALUES (1, '', '')")

        # Migration colonne meteo dans interventions
        cursor.execute("PRAGMA table_info(interventions)")
        interv_cols = {row[1] for row in cursor.fetchall()}
        if 'meteo' not in interv_cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN meteo TEXT DEFAULT NULL")

        # Migration colonnes applicateur/certiphyto/matériel dans exploitation
        cursor.execute("PRAGMA table_info(exploitation)")
        exp_cols = {row[1] for row in cursor.fetchall()}
        for col in ['applicateur', 'certiphyto', 'materiel', 'num_controle', 'date_controle']:
            if col not in exp_cols:
                cursor.execute(f"ALTER TABLE exploitation ADD COLUMN {col} TEXT DEFAULT ''")

        # Table cultures : règles de campagne par culture
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cultures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT NOT NULL UNIQUE,
                code_oepp TEXT DEFAULT '',
                debut_mmdd TEXT NOT NULL DEFAULT '01-01',
                fin_mmdd TEXT NOT NULL DEFAULT '12-31'
            )
        ''')

        # Migration colonne code_oepp si la table existait déjà sans elle
        cursor.execute("PRAGMA table_info(cultures)")
        cult_cols = {row[1] for row in cursor.fetchall()}
        if 'code_oepp' not in cult_cols:
            cursor.execute("ALTER TABLE cultures ADD COLUMN code_oepp TEXT DEFAULT ''")

        cursor.execute("SELECT COUNT(*) FROM cultures")
        if cursor.fetchone()[0] == 0:
            default_cultures = [
                ("Blé tendre d'hiver", "TRZAW", "09-01", "08-31"),
                ("Orge d'hiver", "HORVW", "09-01", "08-31"),
                ("Avoine d'hiver", "AVESA", "09-01", "08-31"),
                ("Triticale", "TTLSS", "09-01", "08-31"),
                ("Colza", "BRSNN", "08-01", "07-31"),
                ("Maïs", "ZEAMX", "01-01", "12-31"),
                ("Tournesol", "HELAN", "01-01", "12-31"),
                ("Soja", "GLXMA", "01-01", "12-31"),
            ]
            cursor.executemany(
                "INSERT INTO cultures (nom, code_oepp, debut_mmdd, fin_mmdd) VALUES (?, ?, ?, ?)",
                default_cultures
            )

        # Migration catalog_products
        new_cols = [
            ("unit",    "TEXT",    "''"),
            ("culture", "TEXT",    "''"),
            ("bbch",    "TEXT",    "''"),
            ("dre",     "INTEGER", "0"),
            ("target",  "TEXT",    "''"),
            ("bio",     "INTEGER", "0"),
            ("dar",     "INTEGER", "0"),
            ("dose_homologuee", "REAL", "0"),
            # Fertilisation : teneurs NPK (% ou unités/unité de produit)
            ("teneur_n", "REAL", "0"),
            ("teneur_p", "REAL", "0"),
            ("teneur_k", "REAL", "0"),
        ]
        cursor.execute("PRAGMA table_info(catalog_products)")
        existing = {row[1] for row in cursor.fetchall()}
        for col, col_type, default in new_cols:
            if col not in existing:
                cursor.execute(f"ALTER TABLE catalog_products ADD COLUMN {col} {col_type} DEFAULT {default}")

        # Table objectifs de fertilisation par culture
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS objectifs_fertilisation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                culture TEXT NOT NULL UNIQUE,
                objectif_n REAL DEFAULT 0,
                objectif_p REAL DEFAULT 0,
                objectif_k REAL DEFAULT 0,
                notes TEXT DEFAULT ''
            )
        ''')

        conn.commit()


# =========================================================================
# ROUTES INTERVENTIONS
# =========================================================================

@interventions_bp.route('/api/interventions', methods=['GET'])
def get_interventions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Migration silencieuse : colonne duration_min
        cursor.execute("PRAGMA table_info(interventions)")
        cols = {row[1] for row in cursor.fetchall()}
        if 'duration_min' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN duration_min INTEGER DEFAULT NULL")
            conn.commit()
        cursor.execute("""
            SELECT device_id, geofence_id, exit_time, vehicle_name,
                   tool_detected, intervention_type, products, applied_area, meteo, duration_min
            FROM interventions
        """)
        rows = cursor.fetchall()

        result = []
        for row in rows:
            item = dict(row)
            try:
                item['products'] = json.loads(item['products']) if item['products'] else []
            except Exception:
                item['products'] = []
            try:
                item['meteo'] = json.loads(item['meteo']) if item['meteo'] else None
            except Exception:
                item['meteo'] = None
            item['appliedArea'] = item['applied_area']
            result.append(item)

    return jsonify(result)


@interventions_bp.route('/api/interventions', methods=['POST'])
def save_intervention():
    data = request.json
    device_id       = data.get('device_id')
    geofence_id     = data.get('geofence_id')
    exit_time       = data.get('exit_time')
    vehicle_name    = data.get('vehicle_name')
    tool_detected   = data.get('tool_detected')
    intervention_type = data.get('intervention_type')
    # Accepte 'appliedArea' (front-end) ou 'applied_area' (fallback)
    applied_area    = data.get('appliedArea') or data.get('applied_area') or 0.0
    products        = json.dumps(data.get('products', []))
    meteo           = json.dumps(data.get('meteo')) if data.get('meteo') else None
    duration_min    = data.get('duration_min') or None

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Migration silencieuse : colonne duration_min
        cursor.execute("PRAGMA table_info(interventions)")
        cols = {row[1] for row in cursor.fetchall()}
        if 'duration_min' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN duration_min INTEGER DEFAULT NULL")

        cursor.execute('''
            INSERT OR REPLACE INTO interventions
            (device_id, geofence_id, exit_time, vehicle_name,
             tool_detected, intervention_type, products, applied_area, meteo, duration_min)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (device_id, geofence_id, exit_time, vehicle_name,
              tool_detected, intervention_type, products, applied_area, meteo, duration_min))
        conn.commit()

    return jsonify({"status": "success"})


# =========================================================================
# ROUTES CATALOGUE PRODUITS
# =========================================================================

@interventions_bp.route('/api/catalog_products', methods=['GET', 'POST', 'DELETE'])
def api_catalog_products():
    if request.method == 'GET':
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k FROM catalog_products ORDER BY name ASC")
            products = [dict(row) for row in cursor.fetchall()]
        return jsonify(products)

    elif request.method == 'POST':
        data    = request.json
        prod_id = data.get('id')
        p_type  = data.get('type')
        name    = data.get('name')
        amm     = data.get('amm', 'N/A')
        dose    = data.get('dose', 0.0)
        unit    = data.get('unit', '')
        culture = data.get('culture', '')
        bbch    = data.get('bbch', '')
        dre     = data.get('dre', 0)
        target  = data.get('target', '')
        bio     = data.get('bio', 0)
        dar     = data.get('dar', 0)
        dose_homologuee = data.get('dose_homologuee', 0)
        teneur_n = float(data.get('teneur_n', 0) or 0)
        teneur_p = float(data.get('teneur_p', 0) or 0)
        teneur_k = float(data.get('teneur_k', 0) or 0)

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if prod_id:
                cursor.execute(
                    "UPDATE catalog_products SET type=?, name=?, amm=?, dose=?, unit=?, culture=?, bbch=?, dre=?, target=?, bio=?, dar=?, dose_homologuee=?, teneur_n=?, teneur_p=?, teneur_k=? WHERE id=?",
                    (p_type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k, prod_id)
                )
            else:
                cursor.execute(
                    "INSERT INTO catalog_products (type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p_type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k)
                )
            conn.commit()
        return jsonify({"status": "success"})

    elif request.method == 'DELETE':
        prod_id = request.args.get('id')
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM catalog_products WHERE id = ?", (prod_id,))
            conn.commit()
        return jsonify({"status": "success"})


@interventions_bp.route('/api/cultures', methods=['GET', 'POST', 'DELETE'])
def api_cultures():
    if request.method == 'GET':
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, nom, code_oepp, debut_mmdd, fin_mmdd FROM cultures ORDER BY nom ASC")
            return jsonify([dict(r) for r in cursor.fetchall()])

    elif request.method == 'POST':
        data = request.json
        culture_id = data.get('id')
        nom = data.get('nom', '').strip()
        code_oepp = data.get('code_oepp', '').strip().upper()
        debut_mmdd = data.get('debut_mmdd', '01-01')
        fin_mmdd = data.get('fin_mmdd', '12-31')
        if not nom:
            return jsonify({"status": "error", "message": "Nom de culture manquant"}), 400

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if culture_id:
                cursor.execute(
                    "UPDATE cultures SET nom=?, code_oepp=?, debut_mmdd=?, fin_mmdd=? WHERE id=?",
                    (nom, code_oepp, debut_mmdd, fin_mmdd, culture_id)
                )
            else:
                cursor.execute(
                    "INSERT OR REPLACE INTO cultures (nom, code_oepp, debut_mmdd, fin_mmdd) VALUES (?, ?, ?, ?)",
                    (nom, code_oepp, debut_mmdd, fin_mmdd)
                )
            conn.commit()
        return jsonify({"status": "success"})

    elif request.method == 'DELETE':
        culture_id = request.args.get('id')
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cultures WHERE id = ?", (culture_id,))
            conn.commit()
        return jsonify({"status": "success"})


@interventions_bp.route('/api/notes_parcelles', methods=['GET', 'POST', 'DELETE'])
def api_notes_parcelles():
    if request.method == 'GET':
        geofence_id = request.args.get('geofence_id')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if geofence_id:
                cursor.execute(
                    "SELECT id, geofence_id, date_note, contenu FROM notes_parcelles WHERE geofence_id = ? ORDER BY date_note DESC",
                    (geofence_id,)
                )
            else:
                cursor.execute(
                    "SELECT id, geofence_id, date_note, contenu FROM notes_parcelles ORDER BY date_note DESC"
                )
            return jsonify([dict(r) for r in cursor.fetchall()])

    elif request.method == 'POST':
        data = request.json
        geofence_id = data.get('geofence_id')
        contenu = (data.get('contenu') or '').strip()
        date_note = data.get('date_note') or datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        if not geofence_id or not contenu:
            return jsonify({"status": "error", "message": "Parcelle ou contenu manquant"}), 400
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO notes_parcelles (geofence_id, date_note, contenu) VALUES (?, ?, ?)",
                (geofence_id, date_note, contenu)
            )
            conn.commit()
        return jsonify({"status": "success"})

    elif request.method == 'DELETE':
        note_id = request.args.get('id')
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM notes_parcelles WHERE id = ?", (note_id,))
            conn.commit()
        return jsonify({"status": "success"})


@interventions_bp.route('/api/interventions/delete', methods=['POST'])
def delete_intervention():
    data = request.json
    device_id   = data.get('device_id')
    geofence_id = data.get('geofence_id')
    exit_time   = data.get('exit_time')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM interventions WHERE device_id=? AND geofence_id=? AND exit_time=?",
            (device_id, geofence_id, exit_time)
        )
        conn.commit()
    return jsonify({"status": "success"})

# =========================================================================
# ROUTES CATALOGUE OUTILS
# =========================================================================

# =========================================================================
# ROUTES PARCELLES
# =========================================================================

@interventions_bp.route('/api/parcelles', methods=['GET', 'POST'])
def api_parcelles():
    if request.method == 'GET':
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT geofence_id, identifiant, nom_parcelle, statut, statut_auto, surface_ha FROM parcelles ORDER BY nom_parcelle ASC")
            return jsonify([dict(r) for r in cursor.fetchall()])
    elif request.method == 'POST':
        data = request.json
        geofence_id  = data.get('geofence_id')
        identifiant  = data.get('identifiant', '')
        nom_parcelle = data.get('nom_parcelle', '')
        statut       = data.get('statut', '')
        statut_auto  = 1 if data.get('statut_auto', True) else 0
        surface_ha   = data.get('surface_ha')
        if surface_ha is not None:
            try: surface_ha = float(str(surface_ha).replace(',', '.'))
            except: surface_ha = None
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO parcelles (geofence_id, identifiant, nom_parcelle, statut, statut_auto, surface_ha)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(geofence_id) DO UPDATE SET
                    identifiant=excluded.identifiant,
                    nom_parcelle=excluded.nom_parcelle,
                    statut=excluded.statut,
                    statut_auto=excluded.statut_auto,
                    surface_ha=excluded.surface_ha
            ''', (geofence_id, identifiant, nom_parcelle, statut, statut_auto, surface_ha))
            conn.commit()
        return jsonify({"status": "success"})

# =========================================================================
# ROUTES EXPLOITATION
# =========================================================================

@interventions_bp.route('/api/exploitation', methods=['GET', 'POST'])
def api_exploitation():
    if request.method == 'GET':
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT siret, raison_sociale, applicateur, certiphyto, materiel, num_controle, date_controle FROM exploitation WHERE id = 1")
            row = cursor.fetchone()
            return jsonify(dict(row) if row else {"siret": "", "raison_sociale": "", "applicateur": "", "certiphyto": "", "materiel": "", "num_controle": "", "date_controle": ""})
    elif request.method == 'POST':
        data = request.json
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE exploitation SET siret=?, raison_sociale=?, applicateur=?, certiphyto=?, materiel=?, num_controle=?, date_controle=? WHERE id=1",
                           (data.get('siret', ''), data.get('raison_sociale', ''),
                            data.get('applicateur', ''), data.get('certiphyto', ''),
                            data.get('materiel', ''), data.get('num_controle', ''), data.get('date_controle', '')))
            conn.commit()
        return jsonify({"status": "success"})

@interventions_bp.route('/api/catalog_tools', methods=['GET', 'POST', 'DELETE'])
def api_catalog_tools():
    if request.method == 'GET':
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, keyword, intervention FROM catalog_tools ORDER BY keyword ASC")
            tools = [dict(row) for row in cursor.fetchall()]
        return jsonify(tools)

    elif request.method == 'POST':
        data    = request.json
        tool_id = data.get('id')
        keyword = data.get('keyword', '').strip().upper()
        intervention = data.get('intervention')

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if tool_id:
                cursor.execute(
                    "UPDATE catalog_tools SET keyword=?, intervention=? WHERE id=?",
                    (keyword, intervention, tool_id)
                )
            else:
                cursor.execute(
                    "INSERT INTO catalog_tools (keyword, intervention) VALUES (?, ?)",
                    (keyword, intervention)
                )
            conn.commit()
        return jsonify({"status": "success"})

    elif request.method == 'DELETE':
        tool_id = request.args.get('id')
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM catalog_tools WHERE id = ?", (tool_id,))
            conn.commit()
        return jsonify({"status": "success"})
