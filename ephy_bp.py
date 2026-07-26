"""
Blueprint e-phy (ANSES) : synchronisation et recherche dans le referentiel officiel des
produits phytosanitaires (data.gouv.fr), pour verifier les numeros AMM et les usages
homologues du catalogue produits.

Extrait de dashboard.py (module totalement autonome, verifie par analyse de dependances --
ses propres tables SQLite, sa propre synchronisation en arriere-plan au demarrage, aucune
autre partie de l'application ne touche a son etat interne). Seule dependance externe reelle :
l'authentification, geree ici via before_request comme pour les autres blueprints plutot que
par un decorateur @login_required importe depuis dashboard.py.
"""
import csv
import io
import re
import sqlite3
import threading
import time
import logging
import unicodedata
import urllib.request
import zipfile
from datetime import datetime

from flask import Blueprint, request, jsonify, session as flask_session

logger = logging.getLogger(__name__)

ephy_bp = Blueprint("ephy", __name__)

# Verrou anti-concurrence : empêche deux synchros e-phy de tourner en même temps.
#
# CORRECTIF : les logs montrent que TOUTES les tentatives de sync échouaient depuis
# plusieurs jours ("Attempt to use ZIP archive that was already closed", "database is
# locked"), et qu'aucune synchro n'avait jamais abouti ("sync OK" absent du log). Cause
# probable : plusieurs synchros lancées en parallèle (sync manuel + synchro
# automatique au démarrage, ou plusieurs clics successifs) se marchent dessus --
# chaque appel à /api/ephy/sync démarrait un nouveau thread sans vérifier qu'une
# synchro était déjà en cours, et plusieurs threads manipulaient le même ZIP en
# mémoire et la même base SQLite simultanément.
_ephy_sync_lock = threading.Lock()
_ephy_sync_in_progress = False


@ephy_bp.before_request
def _require_login():
    """Meme authentification que le reste de l'application (voir interventions.py / ndvi_bp.py
    pour le meme motif)."""
    if not flask_session.get("logged_in"):
        return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401


# =========================================================================
# INTÉGRATION E-PHY (ANSES) — Référentiel produits phytosanitaires
# =========================================================================

# ZIP UTF-8 contenant produits.csv, usages_des_produits_autorises.csv, etc.
EPHY_ZIP_UTF8_URL = "https://www.data.gouv.fr/api/1/datasets/r/98f7cac6-6b29-4859-8739-51b825196959"

def _init_ephy_table():
    """Crée la table ephy_produits si elle n'existe pas, ou migre l'ancien schéma
    (amm en PRIMARY KEY) vers le nouveau (id auto-increment, UNIQUE(amm, nom)).

    CORRECTIF : l'ancien schéma perdait silencieusement les noms commerciaux
    secondaires. Un même numéro AMM peut correspondre à plusieurs produits vendus sous
    des noms différents (même formule, marques distinctes) ; avec "amm" en clé primaire,
    "INSERT OR REPLACE" n'en gardait qu'un seul par AMM lors de chaque synchro (le
    dernier rencontré dans le CSV), faisant chuter le nombre total de produits visibles
    (~15000 lignes du CSV -> ~2679 AMM uniques). La clé unique porte maintenant sur le
    couple (amm, nom) : chaque nom commercial garde sa propre ligne.
    """
    _db = "database.db"
    with sqlite3.connect(_db) as conn:
        cur = conn.cursor()

        # Détecte l'ancien schéma (amm en PRIMARY KEY) pour migrer sans perte des
        # données déjà présentes (même déjà tronquées, en attendant la prochaine
        # synchro complète qui récupérera les noms secondaires manquants).
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ephy_produits'")
        row = cur.fetchone()
        needs_migration = False
        if row and row[0]:
            first_col_def = row[0].split(",")[0].upper()
            needs_migration = "AMM" in first_col_def and "PRIMARY KEY" in first_col_def

        if needs_migration:
            logger.info("e-phy : migration du schéma ephy_produits (amm -> id, UNIQUE(amm, nom))")
            conn.execute("ALTER TABLE ephy_produits RENAME TO ephy_produits_old")
            conn.execute("""
                CREATE TABLE ephy_produits (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    amm           TEXT NOT NULL,
                    nom           TEXT NOT NULL,
                    statut        TEXT,
                    type_produit  TEXT,
                    fonction      TEXT,
                    bio           INTEGER DEFAULT 0,
                    substances    TEXT,
                    dre           INTEGER DEFAULT 0,
                    derniere_maj  TEXT,
                    UNIQUE(amm, nom)
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO ephy_produits (amm,nom,statut,type_produit,fonction,bio,substances,dre,derniere_maj)
                SELECT amm,nom,statut,type_produit,fonction,bio,substances,dre,derniere_maj FROM ephy_produits_old
            """)
            conn.execute("DROP TABLE ephy_produits_old")
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ephy_produits (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    amm           TEXT NOT NULL,
                    nom           TEXT NOT NULL,
                    statut        TEXT,
                    type_produit  TEXT,
                    fonction      TEXT,
                    bio           INTEGER DEFAULT 0,
                    substances    TEXT,
                    dre           INTEGER DEFAULT 0,
                    derniere_maj  TEXT,
                    UNIQUE(amm, nom)
                )
            """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ephy_usages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                amm           TEXT,
                culture       TEXT,
                usage         TEXT,
                dose_min      TEXT,
                dose_max      TEXT,
                unite         TEXT,
                dar           TEXT,
                statut_usage  TEXT,
                bbch_min      TEXT,
                bbch_max      TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ephy_nom ON ephy_produits(nom)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ephy_amm ON ephy_produits(amm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ephy_usages_amm ON ephy_usages(amm)")

        # Migrations silencieuses (colonnes ajoutées après coup)
        cur.execute("PRAGMA table_info(ephy_usages)")
        ephy_u_cols = {r[1] for r in cur.fetchall()}
        if 'bbch_min' not in ephy_u_cols:
            conn.execute("ALTER TABLE ephy_usages ADD COLUMN bbch_min TEXT DEFAULT ''")
        if 'bbch_max' not in ephy_u_cols:
            conn.execute("ALTER TABLE ephy_usages ADD COLUMN bbch_max TEXT DEFAULT ''")

        conn.commit()

_init_ephy_table()


@ephy_bp.route("/api/ephy/search")
def ephy_search():
    """Recherche dans le référentiel e-phy local — insensible aux accents."""
    import unicodedata
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    def strip_accents(s):
        return ''.join(c for c in unicodedata.normalize('NFD', s)
                       if unicodedata.category(c) != 'Mn').upper()

    q_norm = strip_accents(q)

    with sqlite3.connect("database.db") as conn:
        conn.row_factory = sqlite3.Row
        # Créer une fonction SQLite custom pour normaliser
        conn.create_function("strip_acc", 1,
            lambda s: strip_accents(s) if s else "")
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.amm, p.nom, p.statut, p.fonction, p.bio, p.substances, p.dre,
                   GROUP_CONCAT(DISTINCT u.culture) as cultures,
                   MAX(u.usage) as usage,
                   MAX(u.dose_max) as dose,
                   MAX(u.unite) as unite,
                   MAX(u.dar) as dar,
                   MAX(u.bbch_min) as bbch_min,
                   MAX(u.bbch_max) as bbch_max
            FROM ephy_produits p
            LEFT JOIN ephy_usages u ON p.amm = u.amm
            WHERE (strip_acc(p.nom) LIKE ? OR p.amm LIKE ?)
            GROUP BY p.id
            ORDER BY p.nom
            LIMIT 20
        """, (f"%{q_norm}%", f"%{q}%"))
        # NOTE : GROUP BY p.id (et non p.amm comme avant) -- chaque nom commercial est
        # une ligne distincte de ephy_produits depuis le nouveau schéma, donc grouper
        # par amm masquerait de nouveau les noms secondaires dans les résultats de
        # recherche même si la table les contient correctement.
        results = []
        for r in cur.fetchall():
            results.append({
                "amm":       r["amm"],
                "nom":       r["nom"],
                "statut":    r["statut"],
                "fonction":  r["fonction"],
                "bio":       bool(r["bio"]),
                "substances": r["substances"],
                "cultures":  r["cultures"] or "",
                "usage":     r["usage"] or "",
                "dose":      r["dose"] or "",
                "unite":     r["unite"] or "",
                "dar":       r["dar"] or "",
                "bbch_min":  r["bbch_min"] or "",
                "bbch_max":  r["bbch_max"] or "",
                "dre":       r["dre"] or 0,
            })
    return jsonify(results)


@ephy_bp.route("/api/ephy/sync", methods=["POST"])
def ephy_sync():
    """Télécharge et importe les CSV e-phy depuis data.gouv.fr."""
    global _ephy_sync_in_progress
    with _ephy_sync_lock:
        if _ephy_sync_in_progress:
            return jsonify({
                "status": "already_running",
                "message": "Une synchronisation e-phy est déjà en cours, merci de patienter."
            }), 409
        _ephy_sync_in_progress = True

    import threading
    def _do_sync():
        global _ephy_sync_in_progress
        try:
            _sync_ephy()
        finally:
            with _ephy_sync_lock:
                _ephy_sync_in_progress = False
    threading.Thread(target=_do_sync, daemon=True).start()
    return jsonify({"status": "started", "message": "Synchronisation e-phy lancée en arrière-plan."})


@ephy_bp.route("/api/ephy/status")
def ephy_status():
    """Retourne le statut de la base e-phy locale."""
    with sqlite3.connect("database.db") as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as nb FROM ephy_produits")
        nb_produits = cur.fetchone()["nb"]
        cur.execute("SELECT COUNT(DISTINCT amm) as nb FROM ephy_produits")
        nb_amm_uniques = cur.fetchone()["nb"]
        cur.execute("SELECT COUNT(*) as nb FROM ephy_usages")
        nb_usages = cur.fetchone()["nb"]
        cur.execute("SELECT MAX(derniere_maj) as maj FROM ephy_produits")
        row = cur.fetchone()
        derniere_maj = row["maj"] if row else None
    return jsonify({
        "nb_produits": nb_produits,
        "nb_amm_uniques": nb_amm_uniques,
        "nb_usages":   nb_usages,
        "derniere_maj": derniere_maj,
        "synced": nb_produits > 0
    })


def _sync_ephy():
    """
    Télécharge le ZIP e-phy UTF-8 depuis data.gouv.fr,
    extrait produits.csv et usages_des_produits_autorises.csv.
    """
    import csv, io, zipfile, urllib.request
    from datetime import datetime as _dt

    today = _dt.now().strftime("%Y-%m-%d")
    logger.info("e-phy sync démarré")
    print(f"EPHY_DEBUG === sync démarré à {_dt.now()} ===", flush=True)

    try:
        req = urllib.request.Request(
            EPHY_ZIP_UTF8_URL,
            headers={"User-Agent": "DashboardAgricole/12.3"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            zip_data = resp.read()

        logger.info(f"e-phy ZIP téléchargé : {len(zip_data)//1024} Ko")

        produits = []
        usages   = []

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            names = z.namelist()
            logger.info(f"e-phy fichiers : {names}")
            print(f"EPHY_DEBUG fichiers dans le ZIP: {names}", flush=True)

            # produits_Windows-1252.csv
            prod_file = next((n for n in names if 'produits_windows' in n.lower() and 'usage' not in n.lower()), None)
            if not prod_file:
                prod_file = next((n for n in names if 'produits.csv' in n.lower() and 'usage' not in n.lower()), None)
            logger.info(f"e-phy produits file: {prod_file}")
            print(f"EPHY_DEBUG fichier produits detecte: {prod_file}", flush=True)
            if prod_file:
                raw = z.read(prod_file).decode("windows-1252", errors="replace")
                nb_lignes_csv = 0
                nb_rejet_nom_amm = 0
                nb_rejet_retire = 0
                nb_noms_secondaires = 0
                statut_col_vue = None
                from collections import Counter
                statut_counter = Counter()

                # IMPORTANT : produits_Windows-1252.csv contient UNE SEULE ligne par AMM
                # (confirmé par diagnostic : "AMM distincts" == "lignes retenues" avant ce
                # correctif). Les noms commerciaux secondaires (ex: AMM 2180260 = "Carakol"
                # + noms secondaires "Opposum", "Surikate", "Gusto 3", "Taste", "Alfaro",
                # "Balesta") ne sont PAS des lignes séparées avec leur propre statut : ils
                # sont listés à l'intérieur de la colonne "seconds noms commerciaux" de la
                # ligne principale, séparés par " | ". Le filtre RETIRE/AUTORISE s'applique
                # donc bien par AMM (une seule valeur par AMM) -- le bug initial n'était pas
                # là où on pensait : le filtre lui-même est correct, mais les noms
                # secondaires n'étaient simplement jamais extraits ni insérés en base.
                for row in csv.DictReader(io.StringIO(raw), delimiter=";"):
                    nb_lignes_csv += 1
                    nom = (row.get("nom produit") or "").strip()
                    amm = (row.get("numero AMM") or "").strip()
                    if not nom or not amm:
                        nb_rejet_nom_amm += 1
                        continue
                    statut = ""
                    statut_col = None
                    for k in row:
                        if "tat" in k and "autor" in k.lower():
                            statut = (row[k] or "").strip()
                            statut_col = k
                            break
                    if statut_col_vue is None and statut_col:
                        statut_col_vue = statut_col
                    statut_counter[statut] += 1
                    fonction  = (row.get("fonctions") or "").strip()
                    bio       = 0
                    substances = (row.get("Substances actives") or "").strip()
                    type_prod  = (row.get("type produit") or "").strip()
                    # Garder uniquement les AMM non retirés
                    if statut and "RETIRE" in statut.upper():
                        nb_rejet_retire += 1
                        continue
                    produits.append((amm, nom, statut, type_prod, fonction, bio, substances, today))

                    # Extraction des noms commerciaux secondaires du même AMM, pour
                    # qu'ils apparaissent eux aussi dans la recherche (ex: "Taste" doit
                    # remonter comme alias de "Carakol 3", même AMM, même statut).
                    seconds_noms = (row.get("seconds noms commerciaux") or "").strip()
                    if seconds_noms:
                        for nom_secondaire in seconds_noms.split("|"):
                            nom_secondaire = nom_secondaire.strip()
                            if nom_secondaire and nom_secondaire.upper() != nom.upper():
                                produits.append((amm, nom_secondaire, statut, type_prod, fonction, bio, substances, today))
                                nb_noms_secondaires += 1

                print(f"EPHY_DEBUG colonne statut detectee: {statut_col_vue!r}", flush=True)
                print(f"EPHY_DEBUG distribution des statuts (top 15): {statut_counter.most_common(15)}", flush=True)
                print(f"EPHY_DEBUG lignes CSV produits.csv lues: {nb_lignes_csv}", flush=True)
                print(f"EPHY_DEBUG rejetees (nom/amm manquant): {nb_rejet_nom_amm}", flush=True)
                print(f"EPHY_DEBUG rejetees (statut RETIRE): {nb_rejet_retire}", flush=True)
                print(f"EPHY_DEBUG noms secondaires extraits: {nb_noms_secondaires}", flush=True)
                print(f"EPHY_DEBUG retenues avant dedup (noms principaux + secondaires): {len(produits)}", flush=True)
                print(f"EPHY_DEBUG AMM uniques parmi les retenues: {len({p[0] for p in produits})}", flush=True)

            # usages_des_produits_autorises_Windows-1252.csv
            usage_file = next((n for n in names if 'usages_des_produits_autorises' in n.lower()), None)
            if not usage_file:
                usage_file = next((n for n in names if 'produits_usages' in n.lower()), None)
            logger.info(f"e-phy usages file: {usage_file}")
            if usage_file:
                raw2 = z.read(usage_file).decode("windows-1252", errors="replace")
                for row in csv.DictReader(io.StringIO(raw2), delimiter=";"):
                    amm      = (row.get("numero AMM") or "").strip()
                    usage_id = (row.get("identifiant usage") or "").strip()
                    # Extraire la culture = première partie avant le *
                    culture  = usage_id.split("*")[0].strip() if usage_id else ""
                    usage_l  = usage_id
                    dose_max = (row.get("dose retenue") or "").strip()
                    unite    = (row.get("dose retenue unite") or "").strip()
                    dar      = (row.get("delai avant recolte jour") or "").strip()
                    statut_u = (row.get("etat usage") or "Autorise").strip()
                    bbch_min = (row.get("stade cultural min (BBCH)") or "").strip()
                    bbch_max = (row.get("stade cultural max (BBCH)") or "").strip()
                    if amm and culture:
                        usages.append((amm, culture, usage_l, "", dose_max, unite, dar, statut_u, bbch_min, bbch_max))

            # ── produits_condition_emploi : extraire DRE ──
            dre_map = {}  # amm -> dre en heures
            cond_file = next((n for n in names if 'condition_emploi' in n.lower()), None)
            if cond_file:
                import re as _re
                raw3 = z.read(cond_file).decode("windows-1252", errors="replace")
                for row in csv.DictReader(io.StringIO(raw3), delimiter=";"):
                    # Utiliser les valeurs par index pour éviter pb apostrophe typographique
                    vals = list(row.values())
                    if len(vals) < 5: continue
                    amm_c = vals[1].strip()   # numero AMM
                    cat   = vals[3].strip()   # categorie
                    lib   = vals[4].strip()   # libelle
                    if "rentr" in cat.lower():
                        m = _re.search(r"(\d+)\s*heure", lib, _re.IGNORECASE)
                        if m and amm_c:
                            dre_map[amm_c] = int(m.group(1))

        logger.info(f"e-phy DRE : {len(dre_map)} produits avec DRE")

        with sqlite3.connect("database.db", timeout=30) as conn:
            # timeout=30 : filet de sécurité supplémentaire -- si une autre connexion
            # (une requête utilisateur en cours, par exemple) détient un verrou SQLite
            # au même instant, on attend jusqu'à 30s au lieu d'échouer immédiatement
            # avec "database is locked". Le verrou applicatif ci-dessus (_ephy_sync_lock)
            # règle la cause principale (synchros e-phy concurrentes) ; ce timeout
            # couvre les collisions résiduelles avec le reste de l'application.
            conn.execute("DELETE FROM ephy_produits")
            # CORRECTIF : "INSERT OR IGNORE" au lieu de "INSERT OR REPLACE". La table
            # est entièrement vidée juste avant, donc il n'y a plus rien à "remplacer" --
            # OR REPLACE avec l'ancienne clé primaire (amm) est justement ce qui faisait
            # disparaître les noms commerciaux secondaires d'un même AMM. La contrainte
            # UNIQUE(amm, nom) du nouveau schéma protège seulement contre d'éventuels
            # doublons exacts (même amm ET même nom) présents dans le CSV source.
            conn.executemany(
                "INSERT OR IGNORE INTO ephy_produits (amm,nom,statut,type_produit,fonction,bio,substances,dre,derniere_maj) VALUES (?,?,?,?,?,?,?,?,?)",
                [(amm,nom,st,tp,fn,bio,sub, dre_map.get(amm,0), maj)
                 for amm,nom,st,tp,fn,bio,sub,maj in produits])
            conn.execute("DELETE FROM ephy_usages")
            conn.executemany(
                "INSERT INTO ephy_usages (amm,culture,usage,dose_min,dose_max,unite,dar,statut_usage,bbch_min,bbch_max) VALUES (?,?,?,?,?,?,?,?,?,?)",
                usages)
            conn.commit()

        nb_amm_uniques = len({amm for amm, *_ in produits})
        logger.info(f"e-phy sync OK : {len(produits)} produits ({nb_amm_uniques} AMM uniques), {len(usages)} usages")
        print(f"EPHY_DEBUG === sync OK: {len(produits)} produits ({nb_amm_uniques} AMM uniques), {len(usages)} usages ===", flush=True)

    except Exception as e:
        logger.error(f"e-phy sync ERREUR : {e}")
        print(f"EPHY_DEBUG === sync ERREUR: {e} ===", flush=True)


def _ephy_auto_sync():
    """Synchronisation automatique hebdomadaire au démarrage."""
    import time as _time
    from datetime import datetime as _dt

    # Vérifier si une synchro est nécessaire (> 7 jours)
    try:
        with sqlite3.connect("database.db") as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(derniere_maj) as maj FROM ephy_produits")
            row = cur.fetchone()
            if row and row[0]:
                last = _dt.strptime(row[0], "%Y-%m-%d")
                if (_dt.now() - last).days < 7:
                    logger.info("e-phy : synchro récente, pas de mise à jour nécessaire")
                    return
    except Exception:
        pass

    global _ephy_sync_in_progress
    with _ephy_sync_lock:
        if _ephy_sync_in_progress:
            logger.info("e-phy : synchro déjà en cours, auto-sync annulée")
            return
        _ephy_sync_in_progress = True

    logger.info("e-phy : lancement synchro automatique...")
    try:
        _sync_ephy()
    finally:
        with _ephy_sync_lock:
            _ephy_sync_in_progress = False


# Lancer la synchro auto au démarrage avec délai (ne pas bloquer le démarrage Flask)
import threading as _threading
import time as _time

def _ephy_auto_sync_delayed():
    _time.sleep(10)  # Attendre que Flask soit bien démarré
    try:
        _ephy_auto_sync()
    except Exception as e:
        pass  # Ne jamais crasher Flask

_threading.Thread(target=_ephy_auto_sync_delayed, daemon=True).start()



@ephy_bp.route("/api/ephy/check_amm")
def ephy_check_amm():
    """Croise le catalogue avec e-phy pour détecter les AMM retirées."""
    expires = []
    try:
        with sqlite3.connect("database.db") as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT name, amm FROM catalog_products WHERE type='phyto' AND amm IS NOT NULL AND amm != '' AND amm != 'N/A'")
            for prod in cur.fetchall():
                amm = prod['amm'].strip()
                # LIMIT 1 : un même AMM a désormais potentiellement plusieurs lignes
                # (une par nom commercial) depuis le correctif du schéma ; leur statut
                # est identique puisqu'issu de la même fiche produit ANSES, donc une
                # seule suffit pour cette vérification.
                cur.execute("SELECT statut FROM ephy_produits WHERE amm = ? LIMIT 1", (amm,))
                row = cur.fetchone()
                if row is None:
                    expires.append({"name": prod['name'], "amm": amm, "raison": "AMM non trouvee dans e-phy"})
                elif row['statut'] and 'RETIRE' in row['statut'].upper():
                    expires.append({"name": prod['name'], "amm": amm, "raison": "AMM retiree"})
    except Exception as e:
        logger.error(f"ephy_check_amm: {e}")
    return jsonify({"expires": expires, "nb": len(expires)})
