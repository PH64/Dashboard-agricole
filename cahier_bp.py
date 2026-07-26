"""
Blueprint du Cahier de tracabilite PDF : export simple et lisible de toutes les
interventions (tous types, tous produits), groupees par parcelle et sous-parcelle,
accessible depuis l'en-tete du Carnet.

Extrait de dashboard.py. Depend de 4 elements du noyau (DASHBOARD_VERSION,
_ensure_sous_parcelles_table, _get_sous_parcelles_info, build_data), references via
"import dashboard" (jamais "from dashboard import ...") pour toujours voir leur valeur/
comportement a jour -- meme motif que ndvi_bp.py.
"""
import os
import json
import sqlite3
from datetime import datetime

from fpdf import FPDF
from flask import Blueprint, request, jsonify, send_file, redirect, url_for, session as flask_session

import dashboard

cahier_bp = Blueprint("cahier", __name__)


@cahier_bp.before_request
def _require_login():
    """
    Meme authentification que le reste de l'application -- avec la meme distinction que le
    decorateur login_required d'origine : /export_cahier_tracabilite n'est PAS sous /api/
    (c'est un telechargement direct, pas un appel JSON), donc une session expiree doit
    rediriger vers la page de connexion plutot que de renvoyer une erreur JSON brute dans le
    navigateur.
    """
    if not flask_session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401
        return redirect(url_for("login"))


@cahier_bp.route("/export_cahier_tracabilite")
def export_cahier_tracabilite():
    """
    Cahier de traçabilité PDF simple et lisible : toutes les interventions (tous types,
    tous produits -- contrairement au registre phyto réglementaire qui exclut les engrais),
    groupées par parcelle et triées chronologiquement. Filtre optionnel par période via
    ?start=YYYY-MM-DD&end=YYYY-MM-DD.
    """
    DB_PATH = 'database.db'
    start = request.args.get("start")
    end = request.args.get("end")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        query = "SELECT device_id, geofence_id, exit_time, vehicle_name, tool_detected, intervention_type, products, applied_area, sous_parcelle_id FROM interventions"
        conds, params = [], []
        if start:
            conds.append("exit_time >= ?"); params.append(start)
        if end:
            conds.append("exit_time <= ?"); params.append(end + "T23:59:59")
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY geofence_id, exit_time ASC"
        cur.execute(query, params)
        interventions = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, unit FROM catalog_products")
        catalog_units = {r["name"]: r["unit"] for r in cur.fetchall()}

        cur.execute("SELECT geofence_id, identifiant, nom_parcelle, surface_ha FROM parcelles")
        parcelles_info = {str(r["geofence_id"]): dict(r) for r in cur.fetchall()}

        cur.execute("SELECT raison_sociale FROM exploitation WHERE id = 1")
        row = cur.fetchone()
        raison_sociale = row["raison_sociale"] if row else ""

        dashboard._ensure_sous_parcelles_table()
        sous_parcelles_info = dashboard._get_sous_parcelles_info(conn)

    raw = dashboard.build_data()
    geofences_named = raw.get("geofences", {})

    try:
        pdf_path = _generate_cahier_tracabilite_pdf(interventions, catalog_units, parcelles_info, geofences_named, raison_sociale, start, end, sous_parcelles_info)
    except Exception as e:
        app.logger.exception("export_cahier_tracabilite: erreur generation PDF")
        return jsonify({"error": f"Erreur generation PDF : {e}"}), 500

    return send_file(pdf_path, mimetype="application/pdf", as_attachment=True, download_name="cahier_tracabilite.pdf")


def _generate_cahier_tracabilite_pdf(interventions, catalog_units, parcelles_info, geofences_named, raison_sociale, start, end, sous_parcelles_info=None):
    def safe(t):
        return str(t if t is not None else '').encode('latin-1', 'replace').decode('latin-1')

    def date_fr(s):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            return s[:10] if s else "-"

    sous_parcelles_info = sous_parcelles_info or {}

    # Regroupe par parcelle ET sous-parcelle : une parcelle scindée en plusieurs cultures
    # obtient une section distincte par sous-parcelle, plutôt qu'un historique mélangé.
    par_parcelle = {}
    for iv in interventions:
        group_key = (str(iv["geofence_id"]), iv.get("sous_parcelle_id"))
        par_parcelle.setdefault(group_key, []).append(iv)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    page_w = 190

    # Page de garde sobre
    pdf.set_font("Arial", "B", 20)
    pdf.ln(20)
    pdf.cell(0, 14, safe("Cahier de tracabilite"), ln=1, align="C")
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 8, safe("des interventions agricoles"), ln=1, align="C")
    pdf.ln(10)
    pdf.set_font("Arial", "", 11)
    if raison_sociale:
        pdf.cell(0, 7, safe(raison_sociale), ln=1, align="C")
    periode_txt = "Periode : "
    periode_txt += (date_fr(start) if start else "debut") + " au " + (date_fr(end) if end else "aujourd'hui")
    pdf.cell(0, 7, safe(periode_txt), ln=1, align="C")
    pdf.cell(0, 7, safe(f"Genere le {datetime.now().strftime('%d/%m/%Y')}"), ln=1, align="C")
    nb_parcelles_distinctes = len(set(g for g, _ in par_parcelle.keys()))
    pdf.cell(0, 7, safe(f"{len(interventions)} intervention(s) sur {nb_parcelles_distinctes} parcelle(s)"), ln=1, align="C")

    # Une section par parcelle (et sous-parcelle), triee alphabetiquement
    def parcelle_label(geo_id_str, sp_id):
        info = parcelles_info.get(geo_id_str, {})
        nom = info.get("nom_parcelle") or geofences_named.get(geo_id_str, {}).get("name") or f"Parcelle {geo_id_str}"
        surf = info.get("surface_ha")
        sp_info = sous_parcelles_info.get(sp_id) if sp_id else None
        if sp_info:
            nom = f"{nom} - {sp_info['nom']}" + (f" ({sp_info['culture']})" if sp_info.get("culture") else "")
            surf = sp_info.get("surface_ha") or surf
        return nom, surf

    groupes_tries = sorted(par_parcelle.keys(), key=lambda k: parcelle_label(k[0], k[1])[0].lower())

    for geo_id_str, sp_id in groupes_tries:
        nom, surf = parcelle_label(geo_id_str, sp_id)
        ivs = par_parcelle[(geo_id_str, sp_id)]

        pdf.add_page()
        pdf.set_font("Arial", "B", 14)
        pdf.set_fill_color(30, 41, 59); pdf.set_text_color(255, 255, 255)
        titre = f"  {nom}" + (f"  -  {surf} ha" if surf else "")
        pdf.cell(page_w, 10, safe(titre), ln=1, fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

        fill_toggle = False
        for iv in ivs:
            try:
                products = json.loads(iv.get("products") or "[]")
            except Exception:
                products = []
            produits_txt = ", ".join(
                f"{p.get('name','')} ({p.get('dosage','')} {catalog_units.get(p.get('name',''), '')})".strip()
                for p in products if p.get("name")
            ) or "Aucun produit renseigne"
            vehicule_txt = (iv.get("vehicle_name") or "Saisie manuelle")
            if iv.get("tool_detected"):
                vehicule_txt += f"  -  Outil : {iv['tool_detected']}"
            surf_txt = f"{iv['applied_area']} ha" if iv.get("applied_area") else "Surface non renseignee"

            fill_toggle = not fill_toggle

            # Ligne d'en-tête de la fiche : date, type d'intervention, surface
            pdf.set_fill_color(241, 245, 249) if fill_toggle else pdf.set_fill_color(255, 255, 255)
            pdf.set_font("Arial", "B", 10)
            pdf.cell(page_w*0.28, 7, safe(date_fr(iv["exit_time"])), border="LTR", fill=True)
            pdf.set_font("Arial", "B", 10)
            pdf.set_text_color(30, 64, 175)
            pdf.cell(page_w*0.44, 7, safe(iv.get("intervention_type") or "-"), border="TR", fill=True)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Arial", "", 9)
            pdf.cell(page_w*0.28, 7, safe(surf_txt), border="TR", align="R", fill=True)
            pdf.ln()

            # Ligne véhicule/outil
            pdf.set_font("Arial", "", 9)
            pdf.set_text_color(71, 85, 105)
            pdf.cell(page_w, 6, safe("  " + vehicule_txt), border="LR", fill=True)
            pdf.ln()
            pdf.set_text_color(0, 0, 0)

            # Produits (peut passer sur plusieurs lignes automatiquement)
            pdf.set_font("Arial", "", 9)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(page_w, 6, safe("  Produits : " + produits_txt), border="LRB", fill=fill_toggle)
            pdf.ln(3)

    pdf.set_auto_page_break(auto=False)
    pdf.set_y(-15)
    pdf.set_font("Arial", "I", 8)
    pdf.cell(0, 5, safe(f"Cahier de tracabilite genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')} - Dashboard Agricole v{dashboard.DASHBOARD_VERSION}"), align="R")

    os.makedirs('exports', exist_ok=True)
    path = os.path.join('exports', "cahier_tracabilite.pdf")
    pdf.output(path)
    return path
