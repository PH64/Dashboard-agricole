"""
Blueprint Fertilisation : cahier de fertilisation NPK (objectifs par culture, calcul des
apports depuis les interventions, bilan annuel surface traitee vs cadastrale), avec export
PDF. Page /fertilisation, API /api/fertilisation, /api/bilan_annuel,
/api/objectifs_fertilisation, /api/campagnes_disponibles, export /export_pdf_fertilisation.

Extrait de dashboard.py -- non contigu a l origine (3 zones separees par ~2000 lignes,
/api/config_traccar restant sandwiche entre 2 d entre elles est resté dans dashboard.py :
elle modifie un etat global mutable partage par de nombreuses autres fonctions du noyau, la
deplacer aurait rompu ce partage). Depend de build_data(), _ensure_sous_parcelles_table(),
_get_sous_parcelles_info(), find_culture_for_intervention(), get_cultures_rules()
(references via "import dashboard").
"""
import os
import re
import json
import sqlite3
import traceback
from datetime import datetime

from fpdf import FPDF
from flask import Blueprint, request, jsonify, send_file, render_template, redirect, url_for, session as flask_session

import dashboard

fertilisation_bp = Blueprint("fertilisation", __name__)


@fertilisation_bp.before_request
def _require_login():
    """
    Meme authentification que le reste de l'application -- avec la meme distinction que le
    decorateur login_required d'origine : seules les routes /api/... renvoient du JSON,
    /fertilisation (page) et /export_pdf_fertilisation redirigent vers la connexion.
    """
    if not flask_session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401
        return redirect(url_for("login"))


@fertilisation_bp.route("/fertilisation")
def fertilisation():
    import os
    for p in [os.path.join(dashboard.app.template_folder or "templates", "fertilisation.html"),
              os.path.join(os.path.dirname(__file__), "fertilisation.html"),
              "fertilisation.html"]:
        if os.path.isfile(p):
            return send_file(p)
    return render_template("fertilisation.html")

@fertilisation_bp.route("/api/campagnes_disponibles")
def campagnes_disponibles():
    """Liste les couples (culture, campagne) pour lesquels des interventions existent."""
    import traceback
    DB_PATH = 'database.db'
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT geofence_id, exit_time, intervention_type, products, sous_parcelle_id FROM interventions")
            all_interventions = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT name, culture FROM catalog_products")
            products_catalog = {r['name'].strip(): dict(r) for r in cur.fetchall()}
            dashboard._ensure_sous_parcelles_table()
            sous_parcelles_info = dashboard._get_sous_parcelles_info(conn)

        cultures_rules = dashboard.get_cultures_rules()
        combos = set()
        for interv in all_interventions:
            culture_nom, campagne_label = dashboard.find_culture_for_intervention(
                interv['geofence_id'], interv['exit_time'], all_interventions, cultures_rules, products_catalog,
                sous_parcelle_id=interv.get('sous_parcelle_id'), sous_parcelles_info=sous_parcelles_info
            )
            if culture_nom and campagne_label:
                combos.add((culture_nom, campagne_label))

        # Trier par campagne décroissante (plus récente en premier) puis culture alphabétique
        result = sorted(combos, key=lambda x: (x[1], x[0]), reverse=True)
        return jsonify([{"culture": c, "campagne": y} for c, y in result])
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@fertilisation_bp.route("/api/bilan_annuel")
def bilan_annuel():
    """Calcule le bilan par campagne de culture : quantité totale, surfaces, nb interventions, IFT."""
    DB_PATH = 'database.db'
    culture_filter = request.args.get("culture", "")
    campagne_filter = request.args.get("year", "")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT geofence_id, exit_time, intervention_type, products, applied_area, sous_parcelle_id
            FROM interventions
        """)
        all_interventions = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, type, amm, unit, dose, dar, bio, dose_homologuee, culture FROM catalog_products")
        catalog = {r['name'].strip(): dict(r) for r in cur.fetchall()}

        dashboard._ensure_sous_parcelles_table()
        sous_parcelles_info = dashboard._get_sous_parcelles_info(conn)

    cultures_rules = dashboard.get_cultures_rules()

    # Rattacher chaque intervention à sa culture/campagne, puis filtrer
    interventions = []
    for interv in all_interventions:
        culture_nom, campagne_label = dashboard.find_culture_for_intervention(
            interv['geofence_id'], interv['exit_time'], all_interventions, cultures_rules, catalog,
            sous_parcelle_id=interv.get('sous_parcelle_id'), sous_parcelles_info=sous_parcelles_info
        )
        interv['_culture'] = culture_nom
        interv['_campagne'] = campagne_label
        if culture_filter and (culture_nom or '').lower() != culture_filter.lower():
            continue
        if campagne_filter and str(campagne_label) != str(campagne_filter):
            continue
        interventions.append(interv)

    # Surface de chaque parcelle : priorité surface cadastrale DB, sinon nom Traccar
    raw = dashboard.build_data()
    geofences = raw.get("geofences", {})
    surface_parcelle = {}

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur2 = conn.cursor()
        cur2.execute("SELECT geofence_id, surface_ha FROM parcelles WHERE surface_ha IS NOT NULL")
        for r in cur2.fetchall():
            if r['surface_ha']:
                surface_parcelle[int(r['geofence_id'])] = float(r['surface_ha'])

    for gid_str, ginfo in geofences.items():
        gid_int = int(gid_str)
        if gid_int in surface_parcelle:
            continue
        gname = ginfo.get("name", "")
        m = re.search(r'([\d.,]+)\s*ha', gname)
        if m:
            try:
                surface_parcelle[gid_int] = float(m.group(1).replace(',', '.'))
            except (ValueError, TypeError):
                pass

    bilan = {}  # nom_produit -> {type, amm, unit, total_quantity, nb_interventions, surfaces: set, bio}
    ift_par_parcelle = {}  # geofence_id -> somme des (dose appliquee/ha / dose homologuee/ha) * (surface traitee / surface parcelle)
    ift_detail = []        # détail de chaque calcul IFT pour transparence

    for interv in interventions:
        try:
            products = json.loads(interv['products']) if interv['products'] else []
        except Exception:
            products = []

        geo_id = interv.get('geofence_id')
        try:
            surf_traitee = float(str(interv.get('applied_area') or 0).replace(',', '.'))
        except (ValueError, TypeError):
            surf_traitee = 0.0

        for prod in products:
            name = (prod.get('name', '') or '').strip()
            dosage_par_ha = prod.get('dosage', 0) or 0  # dose en L/ha ou kg/ha
            try:
                dosage_par_ha = float(dosage_par_ha)
            except (ValueError, TypeError):
                dosage_par_ha = 0.0
            # cat est un dict FRAIS et ISOLÉ pour CE produit uniquement — jamais partagé entre produits
            cat = dict(catalog.get(name, {}))  # copie défensive pour éviter toute mutation croisée

            if name not in bilan:
                bilan[name] = {
                    'type': cat.get('type', prod.get('type', '')),
                    'amm': cat.get('amm', ''),
                    'unit': cat.get('unit', ''),
                    'bio': bool(cat.get('bio')),
                    'total_quantity': 0.0,   # quantité réelle = dose/ha * surface traitée
                    'nb_interventions': 0,
                    'surfaces': set(),
                }

            # Quantité réelle utilisée pour CETTE intervention = dose/ha * surface traitée (ha).
            # PRIORITÉ à la surface effectivement traitée (saisie sur l'intervention elle-même) :
            # la surface cadastrale de la parcelle ne sert que de repli si l'intervention n'a
            # aucune surface renseignée (sinon on surestime systématiquement la quantité dès
            # qu'une parcelle est traitée partiellement).
            surf_cultivee = surf_traitee if surf_traitee > 0 else surface_parcelle.get(geo_id, 0)
            quantite_utilisee = dosage_par_ha * surf_cultivee
            bilan[name]['total_quantity'] += quantite_utilisee
            bilan[name]['nb_interventions'] += 1
            if geo_id:
                bilan[name]['surfaces'].add(geo_id)

            # IFT (Indice de Fréquence de Traitement), uniquement pour les phyto avec dose homologuée connue
            # Formule officielle : IFT = (dose appliquée / dose homologuée)
            # IMPORTANT : dose_homologuee est un champ DISTINCT de la dose conseillée (dose),
            # car la dose homologuée AMM peut différer de la dose habituellement utilisée.
            # IMPORTANT : si le produit n'est pas trouvé dans le catalogue (cat == {}),
            # ou si sa dose_homologuee n'est pas renseignée, on N'UTILISE JAMAIS la dose
            # homologuée d'un autre produit. On exclut simplement ce produit du calcul IFT.
            if not cat or 'dose_homologuee' not in cat:
                dose_homologuee = 0.0
            else:
                dose_homologuee = cat.get('dose_homologuee') or 0
                try:
                    dose_homologuee = float(dose_homologuee)
                except (ValueError, TypeError):
                    dose_homologuee = 0.0

            if cat.get('type') == 'phyto' and dose_homologuee > 0 and dosage_par_ha > 0:
                # IFT = (dose appliquée / dose homologuée) × (surface traitée / surface cadastrale parcelle)
                # Méthode officielle MASA/GERS
                ratio_dose = dosage_par_ha / dose_homologuee

                # Surface de référence = surface cadastrale DB (prioritaire)
                surf_cadastrale = surface_parcelle.get(geo_id, 0.0)
                # Surface traitée = surface saisie dans l'intervention
                surf_intervention = float(str(interv.get('applied_area') or 0).replace(',', '.') or 0)

                if surf_cadastrale > 0 and surf_intervention > 0:
                    # Rapport surface traitée / surface totale parcelle, plafonné à 1
                    ratio_surface = min(surf_intervention / surf_cadastrale, 1.0)
                elif surf_cadastrale > 0:
                    # Surface intervention inconnue → traitement complet supposé
                    ratio_surface = 1.0
                else:
                    # Surface cadastrale non renseignée → prudence : ratio = 1
                    ratio_surface = 1.0

                ift_contribution = ratio_dose * ratio_surface
                ift_par_parcelle.setdefault(geo_id, 0.0)
                ift_par_parcelle[geo_id] += ift_contribution
                ift_detail.append({
                    'produit': name, 'geofence_id': geo_id,
                    'dose_appliquee': dosage_par_ha, 'dose_homologuee': dose_homologuee,
                    'surface_traitee': round(surf_intervention, 2),
                    'surface_parcelle': round(surf_cadastrale, 2),
                    'ratio_surface': round(ratio_surface, 3),
                    'contribution_ift': round(ift_contribution, 3)
                })

    # Convertir les sets en compte pour la sérialisation JSON
    bilan_list = []
    for name, info in bilan.items():
        bilan_list.append({
            'name': name,
            'type': info['type'],
            'amm': info['amm'],
            'unit': info['unit'],
            'bio': info['bio'],
            'total_quantity': round(info['total_quantity'], 2),
            'nb_interventions': info['nb_interventions'],
            'nb_parcelles': len(info['surfaces']),
        })
    bilan_list.sort(key=lambda x: x['total_quantity'], reverse=True)

    ift_list = [{'geofence_id': k, 'ift': round(v, 2)} for k, v in ift_par_parcelle.items()]
    ift_global = round(sum(ift_par_parcelle.values()) / len(ift_par_parcelle), 2) if ift_par_parcelle else 0

    # Lister les produits phyto utilisés mais SANS dose homologuée (exclus du calcul IFT)
    produits_exclus_ift = sorted(set(
        p['name'] for p in bilan_list
        if p['type'] == 'phyto' and p['name'] not in {d['produit'] for d in ift_detail}
    ))

    return jsonify({
        'culture': culture_filter,
        'campagne': campagne_filter,
        'produits': bilan_list,
        'ift_par_parcelle': ift_list,
        'ift_global': ift_global,
        'ift_detail': ift_detail,
        'produits_exclus_ift': produits_exclus_ift,
        'nb_interventions_total': len(interventions),
    })





# =========================================================================
# ROUTES FERTILISATION
# =========================================================================

@fertilisation_bp.route("/api/objectifs_fertilisation", methods=["GET", "POST"])
def api_objectifs_fertilisation():
    """Lire ou sauvegarder les objectifs NPK par culture."""
    DB_PATH = "database.db"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if request.method == "GET":
            cur.execute("SELECT * FROM objectifs_fertilisation ORDER BY culture")
            return jsonify([dict(r) for r in cur.fetchall()])
        else:
            data = request.get_json()
            culture = data.get("culture", "").strip()
            obj_n   = float(data.get("objectif_n", 0) or 0)
            obj_p   = float(data.get("objectif_p", 0) or 0)
            obj_k   = float(data.get("objectif_k", 0) or 0)
            notes   = data.get("notes", "")
            cur.execute("""
                INSERT INTO objectifs_fertilisation (culture, objectif_n, objectif_p, objectif_k, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(culture) DO UPDATE SET
                    objectif_n=excluded.objectif_n,
                    objectif_p=excluded.objectif_p,
                    objectif_k=excluded.objectif_k,
                    notes=excluded.notes
            """, (culture, obj_n, obj_p, obj_k, notes))
            conn.commit()
            return jsonify({"status": "ok"})


@fertilisation_bp.route("/api/fertilisation")
def api_fertilisation():
    """
    Calcule les apports NPK réels par parcelle/campagne.
    Utilise find_culture_for_intervention (même logique que le bilan/IFT).
    """
    import traceback
    try:
        DB_PATH = "database.db"
        campagne_f = request.args.get("campagne", "")
        culture_f  = request.args.get("culture", "")
        geo_f      = request.args.get("geofence_id", "")

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("""
                SELECT i.device_id, i.geofence_id, i.exit_time,
                       i.intervention_type, i.applied_area, i.products, i.sous_parcelle_id,
                       p.nom_parcelle
                FROM interventions i
                LEFT JOIN parcelles p ON i.geofence_id = p.geofence_id
                ORDER BY i.exit_time ASC
            """)
            interventions = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT name, type, teneur_n, teneur_p, teneur_k, unit, dose, culture FROM catalog_products")
            products_catalog = {r["name"].strip(): dict(r) for r in cur.fetchall()}

            cur.execute("SELECT name, type, teneur_n, teneur_p, teneur_k, unit, dose FROM catalog_products WHERE type = 'engrais'")
            prod_npk = {r["name"]: dict(r) for r in cur.fetchall()}

            cur.execute("SELECT * FROM objectifs_fertilisation")
            objectifs = {r["culture"]: dict(r) for r in cur.fetchall()}

            dashboard._ensure_sous_parcelles_table()
            sous_parcelles_info = dashboard._get_sous_parcelles_info(conn)

        # Utiliser les fonctions existantes du dashboard
        cultures_rules = dashboard.get_cultures_rules()
        all_interv_for_culture = interventions  # toutes les interventions pour find_culture

        data_agg = {}
        # Univers complet des campagnes/cultures, independant des filtres actuellement
        # appliques -- sert uniquement a peupler les menus deroulants du frontend. Sans
        # cette liste separee, choisir un filtre reduit peu a peu les autres menus au sous-
        # ensemble deja filtre (et les vide completement si la combinaison ne donne aucun
        # resultat), empechant l'utilisateur de revenir en arriere sans recharger la page.
        toutes_campagnes_vues = set()
        toutes_cultures_vues = set()
        toutes_parcelles_vues = {}  # geofence_id -> nom_parc (dict pour dedupliquer)

        for iv in interventions:
            prods = []
            try:
                prods = json.loads(iv["products"] or "[]")
            except Exception:
                pass

            engrais = [p for p in prods if p.get("type") == "engrais" and p.get("name")]
            if not engrais:
                continue

            exit_time = iv["exit_time"] or ""
            area      = float(iv["applied_area"] or 0)
            geo_id    = iv["geofence_id"]
            sp_id     = iv.get("sous_parcelle_id")
            sp_info   = sous_parcelles_info.get(sp_id) if sp_id else None
            # Le nom affiché inclut la sous-parcelle quand l'intervention y est rattachée,
            # pour bien distinguer les cultures d'une même parcelle scindée (ex: "Les Grands
            # Champs — Zone Nord" plutôt qu'un seul "Les Grands Champs" ambigu).
            nom_parc_base = iv["nom_parcelle"] or f"Parcelle {geo_id}"
            nom_parc  = f"{nom_parc_base} — {sp_info['nom']}" if sp_info else nom_parc_base

            # ── Utiliser la MÊME logique que le bilan IFT ──
            culture_nom, campagne_label = dashboard.find_culture_for_intervention(
                geo_id, exit_time, all_interv_for_culture, cultures_rules, products_catalog,
                sous_parcelle_id=sp_id, sous_parcelles_info=sous_parcelles_info
            )
            culture   = culture_nom or "—"
            campagne  = campagne_label or ""

            # Si pas de culture trouvée, fallback sur l'année
            if not campagne:
                try:
                    campagne = str(datetime.strptime(exit_time[:10], "%Y-%m-%d").year)
                except Exception:
                    campagne = ""

            # Univers complet, avant filtres -- alimente les menus deroulants cote frontend
            if campagne:
                toutes_campagnes_vues.add(campagne)
            if culture and culture != "—":
                toutes_cultures_vues.add(culture)
            toutes_parcelles_vues[geo_id] = nom_parc

            # Filtres
            if campagne_f and str(campagne) != str(campagne_f): continue
            if culture_f  and culture != culture_f:             continue
            if geo_f      and str(geo_id) != str(geo_f):        continue

            # La clé de regroupement inclut la sous-parcelle : sans ça, deux sous-parcelles
            # de la même géofence portant la même culture (ex: deux variétés de maïs) se
            # seraient fusionnées à tort en une seule ligne, cumulant leurs doses/ha.
            key = (geo_id, sp_id, campagne, culture, nom_parc)
            if key not in data_agg:
                data_agg[key] = {
                    "geofence_id": geo_id, "parcelle": nom_parc,
                    "campagne": campagne, "culture": culture,
                    "surface_ha": 0.0, "apports_n": 0.0,
                    "apports_p": 0.0, "apports_k": 0.0,
                    "nb_apports": 0, "detail": []
                }

            for prod in engrais:
                pname  = prod.get("name", "")
                dosage = float(prod.get("dosage", 0) or 0)
                info   = prod_npk.get(pname, {})
                tn     = float(info.get("teneur_n", 0) or 0)
                tp     = float(info.get("teneur_p", 0) or 0)
                tk     = float(info.get("teneur_k", 0) or 0)
                unit   = info.get("unit", "")

                n_ha = round(dosage * tn / 100, 2) if tn else 0
                p_ha = round(dosage * tp / 100, 2) if tp else 0
                k_ha = round(dosage * tk / 100, 2) if tk else 0

                data_agg[key]["surface_ha"]  = max(data_agg[key]["surface_ha"], area)
                data_agg[key]["apports_n"]  += n_ha * area
                data_agg[key]["apports_p"]  += p_ha * area
                data_agg[key]["apports_k"]  += k_ha * area
                data_agg[key]["nb_apports"] += 1
                data_agg[key]["detail"].append({
                    "date": exit_time[:10], "produit": pname,
                    "dosage": dosage, "unit": unit,
                    "n_ha": n_ha, "p_ha": p_ha, "k_ha": k_ha, "surface": area,
                })

        result = []
        totaux = {"n": 0.0, "p": 0.0, "k": 0.0, "surface": 0.0, "nb": 0}

        for key, v in sorted(data_agg.items(), key=lambda x: (x[0][2] or '', x[0][4] or '')):
            obj   = objectifs.get(v["culture"], {})
            obj_n = float(obj.get("objectif_n", 0) or 0)
            obj_p = float(obj.get("objectif_p", 0) or 0)
            obj_k = float(obj.get("objectif_k", 0) or 0)
            surf  = v["surface_ha"]

            n_ha = round(v["apports_n"] / surf, 1) if surf > 0 else 0
            p_ha = round(v["apports_p"] / surf, 1) if surf > 0 else 0
            k_ha = round(v["apports_k"] / surf, 1) if surf > 0 else 0

            result.append({
                "geofence_id": v["geofence_id"],
                "parcelle":    v["parcelle"],
                "campagne":    v["campagne"],
                "culture":     v["culture"],
                "surface_ha":  round(surf, 2),
                "nb_apports":  v["nb_apports"],
                "n_total":     round(v["apports_n"], 1),
                "p_total":     round(v["apports_p"], 1),
                "k_total":     round(v["apports_k"], 1),
                "n_ha":        n_ha,
                "p_ha":        p_ha,
                "k_ha":        k_ha,
                "objectif_n":  obj_n,
                "objectif_p":  obj_p,
                "objectif_k":  obj_k,
                "pct_n":       round(n_ha / obj_n * 100, 1) if obj_n > 0 else None,
                "pct_p":       round(p_ha / obj_p * 100, 1) if obj_p > 0 else None,
                "pct_k":       round(k_ha / obj_k * 100, 1) if obj_k > 0 else None,
                "detail":      sorted(v["detail"], key=lambda x: x["date"]),
            })
            totaux["n"]       += v["apports_n"]
            totaux["p"]       += v["apports_p"]
            totaux["k"]       += v["apports_k"]
            totaux["surface"] += surf
            totaux["nb"]      += v["nb_apports"]

        all_campagnes = sorted(toutes_campagnes_vues, reverse=True)
        all_cultures  = sorted(toutes_cultures_vues)
        all_parcelles = sorted(
            [{"geofence_id": gid, "parcelle": nom} for gid, nom in toutes_parcelles_vues.items()],
            key=lambda p: p["parcelle"].lower()
        )

        return jsonify({
            "parcelles":     result,
            "totaux":        {k: round(v, 1) for k, v in totaux.items()},
            "objectifs":     objectifs,
            "all_campagnes": all_campagnes,
            "all_cultures":  all_cultures,
            "all_parcelles": all_parcelles,
        })

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@fertilisation_bp.route("/export_pdf_fertilisation")
def export_pdf_fertilisation():
    import traceback
    try:
        DB_PATH = 'database.db'
        campagne_f = request.args.get('campagne', '')
        culture_f  = request.args.get('culture', '')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row; cur = conn.cursor()
            cur.execute("SELECT i.geofence_id,i.exit_time,i.applied_area,i.products,i.sous_parcelle_id,p.nom_parcelle FROM interventions i LEFT JOIN parcelles p ON i.geofence_id=p.geofence_id ORDER BY i.exit_time ASC")
            interventions=[dict(r) for r in cur.fetchall()]
            cur.execute("SELECT name,teneur_n,teneur_p,teneur_k,unit FROM catalog_products WHERE type='engrais'")
            prod_npk={r['name']:dict(r) for r in cur.fetchall()}
            cur.execute("SELECT name,type,amm,unit,culture,bbch,target,bio FROM catalog_products")
            products_catalog={r['name'].strip():dict(r) for r in cur.fetchall()}
            cur.execute("SELECT * FROM objectifs_fertilisation")
            objectifs={r['culture']:dict(r) for r in cur.fetchall()}
            cur.execute("SELECT nom_parcelle,surface_ha FROM parcelles")
            surf_map={r['nom_parcelle']:r['surface_ha'] for r in cur.fetchall() if r['surface_ha']}
            cur.execute("SELECT raison_sociale FROM exploitation WHERE id=1")
            row=cur.fetchone(); exploitation=row['raison_sociale'] if row else ''
            dashboard._ensure_sous_parcelles_table()
            sous_parcelles_info=dashboard._get_sous_parcelles_info(conn)
        cultures_rules=dashboard.get_cultures_rules()
        agg={}
        for iv in interventions:
            prods=[]
            try: prods=json.loads(iv['products'] or '[]')
            except Exception: pass
            engrais=[p for p in prods if p.get('type')=='engrais' and p.get('name')]
            if not engrais: continue
            geo_id=iv['geofence_id']
            sp_id=iv.get('sous_parcelle_id')
            sp_info=sous_parcelles_info.get(sp_id) if sp_id else None
            nom_parc_base=iv['nom_parcelle'] or f"Parcelle {geo_id}"
            nom_parc=f"{nom_parc_base} — {sp_info['nom']}" if sp_info else nom_parc_base
            area=float(iv['applied_area'] or 0); exit_t=iv['exit_time'] or ''
            culture_nom,campagne_label=dashboard.find_culture_for_intervention(geo_id,exit_t,interventions,cultures_rules,products_catalog,sous_parcelle_id=sp_id,sous_parcelles_info=sous_parcelles_info)
            culture=culture_nom or '—'; campagne=campagne_label or ''
            if campagne_f and str(campagne)!=str(campagne_f): continue
            if culture_f  and culture!=culture_f: continue
            key=(nom_parc,sp_id,campagne,culture)
            if key not in agg: agg[key]={'surface':area,'apports':[],'n':0.0,'p':0.0,'k':0.0}
            agg[key]['surface']=max(agg[key]['surface'],area)
            for prod in engrais:
                pname=prod.get('name',''); dosage=float(prod.get('dosage',0) or 0)
                info=prod_npk.get(pname,{})
                tn=float(info.get('teneur_n',0) or 0); tp=float(info.get('teneur_p',0) or 0); tk=float(info.get('teneur_k',0) or 0)
                n_ha=round(dosage*tn/100,1) if tn else 0; p_ha=round(dosage*tp/100,1) if tp else 0; k_ha=round(dosage*tk/100,1) if tk else 0
                agg[key]['n']+=n_ha; agg[key]['p']+=p_ha; agg[key]['k']+=k_ha
                agg[key]['apports'].append({'date':exit_t[:10],'produit':pname,'dosage':dosage,'unit':info.get('unit',''),'n_ha':n_ha,'p_ha':p_ha,'k_ha':k_ha})
        def safe(t): return str(t or '').encode('latin-1','replace').decode('latin-1')
        pdf=FPDF(orientation='P',unit='mm',format='A4'); pdf.set_auto_page_break(auto=True,margin=15); pw=190
        pdf.add_page()
        pdf.set_fill_color(34,197,94); pdf.rect(0,0,210,40,'F')
        pdf.set_font('Arial','B',22); pdf.set_text_color(255,255,255); pdf.set_y(10)
        pdf.cell(0,12,safe('Cahier de Fertilisation'),align='C',ln=1)
        pdf.set_font('Arial','',13); pdf.cell(0,8,safe(exploitation),align='C',ln=1)
        pdf.set_text_color(0,0,0)
        filters=[]
        if campagne_f: filters.append(f"Campagne : {campagne_f}")
        if culture_f: filters.append(f"Culture : {culture_f}")
        if filters: pdf.set_y(50); pdf.set_font('Arial','',11); pdf.cell(0,7,safe(' | '.join(filters)),align='C',ln=1)
        for (nom_parc,sp_id,campagne,culture),v in sorted(agg.items(), key=lambda x: (x[0][2] or '', x[0][0] or '')):
            pdf.add_page()
            pdf.set_fill_color(34,197,94); pdf.set_text_color(255,255,255); pdf.set_font('Arial','B',13)
            pdf.cell(0,9,safe(f"  {nom_parc}"),ln=1,fill=True); pdf.set_text_color(0,0,0); pdf.ln(2)
            surf_cad=surf_map.get(nom_parc,v['surface']); obj=objectifs.get(culture,{})
            pdf.set_fill_color(240,253,244); pdf.set_font('Arial','',10)
            for label,val in [('Campagne',campagne),('Culture',culture),('Surface',f"{surf_cad} ha" if surf_cad else '—')]:
                pdf.cell(50,7,safe(f"  {label} :"),fill=True); pdf.cell(0,7,safe(f"  {val}"),fill=True,ln=1)
            pdf.ln(3)
            pdf.set_fill_color(34,197,94); pdf.set_text_color(255,255,255); pdf.set_font('Arial','B',9)
            cols=['Date','Produit','Dose/ha','N (U/ha)','P (U/ha)','K (U/ha)']; widths=[25,55,25,28,28,28]
            for i,col in enumerate(cols): pdf.cell(widths[i],7,safe(col),border=1,align='C',fill=True)
            pdf.ln(); pdf.set_text_color(0,0,0); pdf.set_font('Arial','',9); fill=False
            for ap in sorted(v['apports'],key=lambda x:x['date']):
                pdf.set_fill_color(240,253,244) if fill else pdf.set_fill_color(255,255,255)
                for i,val in enumerate([ap['date'],ap['produit'],f"{ap['dosage']} {ap['unit']}",str(ap['n_ha']) if ap['n_ha'] else '—',str(ap['p_ha']) if ap['p_ha'] else '—',str(ap['k_ha']) if ap['k_ha'] else '—']):
                    pdf.cell(widths[i],6,safe(val),border=1,fill=fill)
                pdf.ln(); fill=not fill
            pdf.set_fill_color(220,252,231); pdf.set_font('Arial','B',9)
            pdf.cell(widths[0]+widths[1],7,safe('  TOTAL APPORTE'),border=1,fill=True)
            pdf.cell(widths[2],7,'',border=1,fill=True)
            for vi in [v['n'],v['p'],v['k']]: pdf.cell(widths[3],7,safe(f"{round(vi,1)} U"),border=1,align='C',fill=True)
            pdf.ln()
            obj_n=float(obj.get('objectif_n',0) or 0)
            if obj_n>0:
                pdf.ln(2); pdf.set_fill_color(254,243,199); pdf.set_font('Arial','B',9)
                pct_n=round(v['n']/obj_n*100,1)
                pdf.cell(80,7,safe(f"  Objectif N : {obj_n} U/ha"),border=1,fill=True)
                pdf.cell(55,7,safe(f"  Réalisé : {round(v['n'],1)} U/ha"),border=1,fill=True)
                pdf.cell(55,7,safe(f"  Taux : {pct_n}%"),border=1,fill=True); pdf.ln()
        pdf.set_y(-15); pdf.set_font('Arial','I',8)
        from datetime import datetime as dt2
        pdf.cell(0,5,safe(f"Imprimé le {dt2.now().strftime('%d/%m/%Y à %H:%M')}"),align='R')
        os.makedirs('exports',exist_ok=True); path=os.path.join('exports','fertilisation.pdf')
        pdf.output(path); return send_file(path,as_attachment=True,download_name='cahier_fertilisation.pdf')
    except Exception as e:
        return f"Erreur PDF fertilisation : {e}<br><pre>{__import__('traceback').format_exc()}</pre>",500
