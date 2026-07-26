import sqlite3
import json
from datetime import datetime
from flask import Blueprint, request, jsonify, session as flask_session

interventions_bp = Blueprint('interventions', __name__)
DB_PATH = 'database.db'


@interventions_bp.before_request
def _require_login():
    """
    Protège TOUTES les routes de ce blueprint derrière la même authentification que le
    reste de l'application (dashboard.py). Sans ce garde-fou, les routes /api/interventions,
    /api/catalog_products, /api/parcelles, /api/exploitation, etc. étaient accessibles sans
    connexion -- un décorateur @login_required par route n'avait jamais été appliqué ici.
    """
    if not flask_session.get('logged_in'):
        return jsonify({"error": "Non authentifié", "redirect": "/login"}), 401


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
            ("actif",    "INTEGER", "1"),
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

        # Migration colonne rendement (t/ha) sur les interventions de type Récolte
        cursor.execute("PRAGMA table_info(interventions)")
        interv_cols2 = {row[1] for row in cursor.fetchall()}
        if 'rendement' not in interv_cols2:
            cursor.execute("ALTER TABLE interventions ADD COLUMN rendement REAL DEFAULT NULL")

        # Migration colonnes stock produit (quantité restante + seuil d'alerte)
        cursor.execute("PRAGMA table_info(catalog_products)")
        prod_cols2 = {row[1] for row in cursor.fetchall()}
        if 'stock' not in prod_cols2:
            cursor.execute("ALTER TABLE catalog_products ADD COLUMN stock REAL DEFAULT NULL")
        if 'seuil_alerte_stock' not in prod_cols2:
            cursor.execute("ALTER TABLE catalog_products ADD COLUMN seuil_alerte_stock REAL DEFAULT NULL")

        # Migration colonnes besoins phénologiques (semences) : sommes de températures
        # base 6°C - plafond 30°C, du semis à la floraison / à la maturité, propres à
        # chaque variété (données semencier).
        cursor.execute("PRAGMA table_info(catalog_products)")
        prod_cols3 = {row[1] for row in cursor.fetchall()}
        if 'besoin_floraison' not in prod_cols3:
            cursor.execute("ALTER TABLE catalog_products ADD COLUMN besoin_floraison REAL DEFAULT NULL")
        if 'besoin_maturite' not in prod_cols3:
            cursor.execute("ALTER TABLE catalog_products ADD COLUMN besoin_maturite REAL DEFAULT NULL")

        # Migration de compatibilité : la fonctionnalité s'appelait "sous-zone" à sa création
        # et a été renommée en "sous-parcelle" -- si l'ancienne table/colonne existe encore
        # (installation mise à jour depuis une version antérieure), on les renomme plutôt que
        # d'en recréer de nouvelles vides, pour ne perdre aucune donnée déjà saisie.
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor.fetchall()}
        if 'sous_zones' in existing_tables and 'sous_parcelles' not in existing_tables:
            cursor.execute("ALTER TABLE sous_zones RENAME TO sous_parcelles")
        cursor.execute("PRAGMA table_info(interventions)")
        _interv_cols_migration = {row[1] for row in cursor.fetchall()}
        if 'sous_zone_id' in _interv_cols_migration and 'sous_parcelle_id' not in _interv_cols_migration:
            cursor.execute("ALTER TABLE interventions RENAME COLUMN sous_zone_id TO sous_parcelle_id")

        # Sous-parcelles : permettent de scinder une parcelle Traccar en plusieurs cultures pour
        # une campagne donnée, SANS jamais toucher à la géofence Traccar elle-même (qui reste
        # la seule référence pour l'historique, la carte de chantier, etc.). Une sous-parcelle a
        # son propre polygone (dessiné dans l'appli), sa propre culture/statut/surface.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sous_parcelles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geofence_id TEXT NOT NULL,
                campagne TEXT NOT NULL,
                nom TEXT NOT NULL,
                culture TEXT DEFAULT '',
                statut TEXT DEFAULT 'attente',
                surface_ha REAL,
                polygon TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')

        # Migration colonne sous_parcelle_id sur les interventions : rattache optionnellement
        # une intervention à une sous-parcelle plutôt qu'à la parcelle entière (NULL = comme
        # avant, l'intervention concerne toute la parcelle).
        cursor.execute("PRAGMA table_info(interventions)")
        interv_cols3 = {row[1] for row in cursor.fetchall()}
        if 'sous_parcelle_id' not in interv_cols3:
            cursor.execute("ALTER TABLE interventions ADD COLUMN sous_parcelle_id INTEGER DEFAULT NULL")

        # Index sur les colonnes les plus filtrées/groupées de "interventions" -- la table la
        # plus sollicitée de l'application (phénologie, bilan, fertilisation, carnet, synthèse
        # la parcourent tous). Sans index, chaque requête filtrée par parcelle ou triée par
        # date fait un balayage complet de la table ; ça reste rapide avec peu de lignes, mais
        # se dégrade avec plusieurs campagnes d'historique accumulées.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_interventions_geofence ON interventions(geofence_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_interventions_sous_parcelle ON interventions(sous_parcelle_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_interventions_exit_time ON interventions(exit_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sous_parcelles_geofence ON sous_parcelles(geofence_id)")

        conn.commit()


# =========================================================================
# ROUTES INTERVENTIONS
# =========================================================================

@interventions_bp.route('/api/interventions', methods=['GET'])
def get_interventions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Migration silencieuse : colonnes duration_min, rendement et sous_parcelle_id
        cursor.execute("PRAGMA table_info(interventions)")
        cols = {row[1] for row in cursor.fetchall()}
        if 'duration_min' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN duration_min INTEGER DEFAULT NULL")
            conn.commit()
        if 'rendement' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN rendement REAL DEFAULT NULL")
            conn.commit()
        if 'sous_parcelle_id' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN sous_parcelle_id INTEGER DEFAULT NULL")
            conn.commit()
        cursor.execute("""
            SELECT device_id, geofence_id, exit_time, vehicle_name,
                   tool_detected, intervention_type, products, applied_area, meteo, duration_min, rendement, sous_parcelle_id
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


def _ajuster_stock_produits(conn, products_json_str, applied_area, sens):
    """
    Ajuste le stock des produits catalogue (engrais/phyto, peu importe leur type -- la
    recherche se fait par nom exact, meme convention que le reste de l'application) suite
    a l'enregistrement ou la suppression d'une intervention.

    sens = -1 pour consommer (nouvelle intervention ou intervention modifiee), +1 pour
    restaurer (avant d'ecraser une intervention existante avec de nouvelles valeurs, ou
    lors de sa suppression) -- une modification d'intervention appelle donc cette fonction
    DEUX fois : d'abord avec +1 sur les anciennes valeurs (annule la consommation
    precedente), puis avec -1 sur les nouvelles (applique la nouvelle consommation). Ce
    motif "annuler puis reappliquer" evite d'avoir a calculer un delta explicite entre
    ancien et nouveau produit/dosage/surface.

    Ne touche JAMAIS un produit dont le stock est NULL (= stock non suivi pour ce produit,
    choix explicite de l'utilisateur de ne pas le gerer) : seuls les produits ayant deja
    une valeur de stock renseignee (meme 0) sont ajustes, pour ne jamais faire apparaitre
    un stock negatif ou une fausse alerte sur un produit jamais suivi.

    N'ajuste rien si applied_area est absente ou nulle : le dosage catalogue est exprime
    par hectare, donc sans surface connue la quantite reellement consommee ne peut pas
    etre calculee de facon fiable -- mieux vaut ne rien faire que de faire une hypothese
    arbitraire (par exemple supposer 1 ha).
    """
    if not applied_area:
        return
    try:
        applied_area = float(applied_area)
    except (TypeError, ValueError):
        return
    if applied_area <= 0:
        return

    try:
        produits = json.loads(products_json_str or "[]")
    except Exception:
        return
    if not produits:
        return

    cursor = conn.cursor()
    for p in produits:
        nom = (p.get("name") or "").strip()
        if not nom:
            continue
        try:
            dosage = float(str(p.get("dosage") or 0).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if dosage <= 0:
            continue

        quantite = round(dosage * applied_area * sens, 3)
        cursor.execute(
            "UPDATE catalog_products SET stock = stock + ? WHERE name = ? AND stock IS NOT NULL",
            (quantite, nom)
        )


@interventions_bp.route('/api/interventions', methods=['POST'])
def save_intervention():
    data = request.json or {}
    device_id       = data.get('device_id')
    geofence_id     = data.get('geofence_id')
    exit_time       = data.get('exit_time')

    # device_id, geofence_id et exit_time sont NOT NULL en base : sans ce garde-fou, un
    # payload incomplet provoquait un crash 500 (IntegrityError) au lieu d'un message clair.
    if device_id is None or geofence_id is None or not exit_time:
        return jsonify({"status": "error", "message": "device_id, geofence_id et exit_time sont obligatoires"}), 400

    vehicle_name    = data.get('vehicle_name')
    tool_detected   = data.get('tool_detected')
    intervention_type = data.get('intervention_type')
    # Accepte 'appliedArea' (front-end) ou 'applied_area' (fallback)
    applied_area    = data.get('appliedArea') or data.get('applied_area') or 0.0
    products        = json.dumps(data.get('products', []))
    meteo           = json.dumps(data.get('meteo')) if data.get('meteo') else None
    duration_min    = data.get('duration_min') or None
    sous_parcelle_id    = data.get('sous_parcelle_id') or None
    rendement       = data.get('rendement')
    if rendement is not None:
        try: rendement = float(str(rendement).replace(',', '.'))
        except (TypeError, ValueError): rendement = None

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Migration silencieuse : colonnes duration_min, rendement et sous_parcelle_id
        cursor.execute("PRAGMA table_info(interventions)")
        cols = {row[1] for row in cursor.fetchall()}
        if 'duration_min' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN duration_min INTEGER DEFAULT NULL")
        if 'rendement' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN rendement REAL DEFAULT NULL")
        if 'sous_parcelle_id' not in cols:
            cursor.execute("ALTER TABLE interventions ADD COLUMN sous_parcelle_id INTEGER DEFAULT NULL")

        # Recupere l'intervention existante (le cas echeant) AVANT de l'ecraser, pour
        # pouvoir restaurer le stock qu'elle avait consomme -- INSERT OR REPLACE ecrase
        # silencieusement l'ancienne ligne juste apres, qui ne serait sinon plus jamais
        # consultable pour annuler sa consommation.
        cursor.execute(
            "SELECT products, applied_area FROM interventions WHERE device_id=? AND geofence_id=? AND exit_time=?",
            (device_id, geofence_id, exit_time)
        )
        ancienne = cursor.fetchone()
        if ancienne:
            _ajuster_stock_produits(conn, ancienne[0], ancienne[1], +1)

        cursor.execute('''
            INSERT OR REPLACE INTO interventions
            (device_id, geofence_id, exit_time, vehicle_name,
             tool_detected, intervention_type, products, applied_area, meteo, duration_min, rendement, sous_parcelle_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (device_id, geofence_id, exit_time, vehicle_name,
              tool_detected, intervention_type, products, applied_area, meteo, duration_min, rendement, sous_parcelle_id))

        # Ajuste le stock des produits utilises. L'ancienne ligne (le cas echeant, si cette
        # sauvegarde remplace une intervention existante -- meme cle device_id/geofence_id/
        # exit_time) a deja ete recuperee et restauree AVANT le INSERT OR REPLACE ci-dessus,
        # qui l'ecrase silencieusement (voir plus haut). Seule la nouvelle consommation est
        # appliquee ici.
        _ajuster_stock_produits(conn, products, applied_area, -1)

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
            cursor.execute("SELECT id, type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k, actif, stock, seuil_alerte_stock, besoin_floraison, besoin_maturite FROM catalog_products ORDER BY name ASC")
            products = [dict(row) for row in cursor.fetchall()]
        return jsonify(products)

    elif request.method == 'POST':
        data    = request.json or {}
        prod_id = data.get('id')
        p_type  = data.get('type')
        name    = (data.get('name') or '').strip()

        # 'name' est NOT NULL en base : sans ce garde-fou, un payload sans nom provoquait
        # un crash 500 (IntegrityError) au lieu d'un message clair.
        if not name:
            return jsonify({"status": "error", "message": "Le nom du produit est obligatoire"}), 400

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
        actif = int(data.get('actif', 1))

        def _to_float_or_none(v):
            if v is None or v == '':
                return None
            try: return float(str(v).replace(',', '.'))
            except (TypeError, ValueError): return None

        stock = _to_float_or_none(data.get('stock'))
        seuil_alerte_stock = _to_float_or_none(data.get('seuil_alerte_stock'))
        besoin_floraison = _to_float_or_none(data.get('besoin_floraison'))
        besoin_maturite = _to_float_or_none(data.get('besoin_maturite'))

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if prod_id:
                cursor.execute(
                    "UPDATE catalog_products SET type=?, name=?, amm=?, dose=?, unit=?, culture=?, bbch=?, dre=?, target=?, bio=?, dar=?, dose_homologuee=?, teneur_n=?, teneur_p=?, teneur_k=?, actif=?, stock=?, seuil_alerte_stock=?, besoin_floraison=?, besoin_maturite=? WHERE id=?",
                    (p_type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k, actif, stock, seuil_alerte_stock, besoin_floraison, besoin_maturite, prod_id)
                )
            else:
                cursor.execute(
                    "INSERT INTO catalog_products (type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k, actif, stock, seuil_alerte_stock, besoin_floraison, besoin_maturite) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p_type, name, amm, dose, unit, culture, bbch, dre, target, bio, dar, dose_homologuee, teneur_n, teneur_p, teneur_k, actif, stock, seuil_alerte_stock, besoin_floraison, besoin_maturite)
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


@interventions_bp.route('/api/catalog_products/<int:prod_id>/ajuster_stock', methods=['POST'])
def ajuster_stock_produit(prod_id):
    """
    Corrige le stock d'un produit a une valeur reelle constatee (inventaire physique),
    independamment du reste de sa fiche (nom, dose, AMM...) et du stock theorique calcule
    automatiquement au fil des interventions -- utile pour recaler apres un ecart (erreur
    de mesure, casse, perte, ajout d'un nouvel achat...). Contrairement a la route complete
    POST /api/catalog_products, celle-ci ne touche qu'au stock, sans avoir a renvoyer tous
    les autres champs du produit.
    """
    data = request.json or {}
    try:
        nouveau_stock = float(str(data.get('stock')).replace(',', '.'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Stock invalide"}), 400

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE catalog_products SET stock = ? WHERE id = ?", (nouveau_stock, prod_id))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"status": "error", "message": "Produit introuvable"}), 404
    return jsonify({"status": "success", "stock": nouveau_stock})


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
        # Restaure le stock consomme par cette intervention avant de la supprimer -- sans
        # quoi le stock resterait diminue pour un traitement qui n'a finalement jamais eu
        # lieu (ou dont la saisie a ete annulee/corrigee par une suppression).
        cursor.execute(
            "SELECT products, applied_area FROM interventions WHERE device_id=? AND geofence_id=? AND exit_time=?",
            (device_id, geofence_id, exit_time)
        )
        existante = cursor.fetchone()
        if existante:
            _ajuster_stock_produits(conn, existante[0], existante[1], +1)

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
            except Exception: surface_ha = None
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
# ROUTES SOUS-ZONES (scinder une parcelle en plusieurs cultures pour une
# campagne donnée, sans jamais toucher à la géofence Traccar elle-même)
# =========================================================================

@interventions_bp.route('/api/sous_parcelles', methods=['GET', 'POST'])
def api_sous_parcelles():
    if request.method == 'GET':
        geofence_id = request.args.get('geofence_id')
        campagne = request.args.get('campagne')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = "SELECT * FROM sous_parcelles"
            conds, params = [], []
            if geofence_id:
                conds.append("geofence_id = ?"); params.append(str(geofence_id))
            if campagne:
                conds.append("campagne = ?"); params.append(campagne)
            if conds:
                query += " WHERE " + " AND ".join(conds)
            query += " ORDER BY nom ASC"
            cursor.execute(query, params)
            rows = []
            for r in cursor.fetchall():
                row = dict(r)
                try:
                    row["polygon"] = json.loads(row["polygon"])
                except Exception:
                    row["polygon"] = []
                rows.append(row)
            return jsonify(rows)

    elif request.method == 'POST':
        data = request.json or {}
        sz_id = data.get('id')
        geofence_id = data.get('geofence_id')
        campagne = (data.get('campagne') or '').strip()
        nom = (data.get('nom') or '').strip()
        culture = data.get('culture', '')
        statut = data.get('statut', 'attente')
        surface_ha = data.get('surface_ha')
        polygon = data.get('polygon')  # liste de [lat, lon]

        if not geofence_id or not campagne or not nom or not polygon or len(polygon) < 3:
            return jsonify({"status": "error", "message": "geofence_id, campagne, nom et un polygone (3 points min.) sont obligatoires"}), 400

        if surface_ha is not None:
            try: surface_ha = float(str(surface_ha).replace(',', '.'))
            except (TypeError, ValueError): surface_ha = None

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if sz_id:
                cursor.execute('''
                    UPDATE sous_parcelles SET geofence_id=?, campagne=?, nom=?, culture=?, statut=?, surface_ha=?, polygon=?
                    WHERE id=?
                ''', (str(geofence_id), campagne, nom, culture, statut, surface_ha, json.dumps(polygon), sz_id))
            else:
                cursor.execute('''
                    INSERT INTO sous_parcelles (geofence_id, campagne, nom, culture, statut, surface_ha, polygon, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (str(geofence_id), campagne, nom, culture, statut, surface_ha, json.dumps(polygon), datetime.now().strftime('%Y-%m-%dT%H:%M:%S')))
            conn.commit()
            new_id = sz_id or cursor.lastrowid
        return jsonify({"status": "success", "id": new_id})


@interventions_bp.route('/api/sous_parcelles/<int:sz_id>', methods=['PUT', 'DELETE'])
def api_sous_parcelle_detail(sz_id):
    if request.method == 'DELETE':
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Les interventions liées à cette sous-parcelle ne sont pas supprimées : elles
            # redeviennent simplement rattachées à la parcelle entière (sous_parcelle_id -> NULL).
            cursor.execute("UPDATE interventions SET sous_parcelle_id = NULL WHERE sous_parcelle_id = ?", (sz_id,))
            cursor.execute("DELETE FROM sous_parcelles WHERE id = ?", (sz_id,))
            conn.commit()
        return jsonify({"status": "success"})

    elif request.method == 'PUT':
        data = request.json or {}
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if 'statut' in data:
                cursor.execute("UPDATE sous_parcelles SET statut = ? WHERE id = ?", (data['statut'], sz_id))
            if 'culture' in data:
                cursor.execute("UPDATE sous_parcelles SET culture = ? WHERE id = ?", (data['culture'], sz_id))
            if 'nom' in data:
                cursor.execute("UPDATE sous_parcelles SET nom = ? WHERE id = ?", (data['nom'], sz_id))
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
