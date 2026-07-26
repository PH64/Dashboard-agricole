"""
Blueprint du module Analytique : statistiques et graphiques (surfaces travaillees, vitesses,
produits utilises, historique par parcelle/sous-parcelle...) sur une periode donnee, avec
export PDF.

Extrait de dashboard.py -- non contigu a l origine (page /analytique, /api/analytique et
/export_pdf_analytique etaient a plus de 3000 lignes d ecart les unes des autres), regroupe
ici car thematiquement une seule fonctionnalite. Depend de build_data(),
_ensure_sous_parcelles_table() et _get_sous_parcelles_info() (references via
"import dashboard", meme motif que ndvi_bp.py/cahier_bp.py).
"""
import os
import re
import json
import sqlite3
from datetime import datetime

from fpdf import FPDF
from flask import Blueprint, request, jsonify, send_file, render_template, redirect, url_for, session as flask_session

import dashboard

analytique_bp = Blueprint("analytique", __name__)


@analytique_bp.before_request
def _require_login():
    """
    Meme authentification que le reste de l'application -- avec la meme distinction que le
    decorateur login_required d'origine : seule /api/analytique est sous /api/ (reponse JSON),
    /analytique et /export_pdf_analytique redirigent vers la connexion comme des pages/
    telechargements directs.
    """
    if not flask_session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401
        return redirect(url_for("login"))


@analytique_bp.route("/analytique")
def analytique():
    import os
    # Cherche dans templates/ puis dans le dossier courant
    for p in [os.path.join(dashboard.app.template_folder or "templates", "analytique.html"),
              os.path.join(os.path.dirname(__file__), "analytique.html"),
              "analytique.html"]:
        if os.path.isfile(p):
            return send_file(p)
    return render_template("analytique.html")

@analytique_bp.route("/api/analytique")
def api_analytique():
    try:
        return _api_analytique_inner()
    except Exception as exc:
        import traceback
        return jsonify({"error": str(exc), "trace": traceback.format_exc()}), 500

def _api_analytique_inner():
    DB_PATH   = 'database.db'
    start_str = request.args.get("start", "")
    end_str   = request.args.get("end",   "")
    f_vehicle = request.args.get("vehicle", "")
    f_geo     = request.args.get("geofence", "")
    f_tool    = request.args.get("outil", "")
    f_type    = request.args.get("intervention_type", "")

    # ── Migration silencieuse ──
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(interventions)")
        cols = {r[1] for r in cur.fetchall()}
        if "duration_min" not in cols:
            cur.execute("ALTER TABLE interventions ADD COLUMN duration_min INTEGER DEFAULT NULL")
        conn.commit()

    # ── Enrichir duration_min depuis Traccar (cache) ──
    try:
        raw    = dashboard.build_data()
        events = raw.get("events", [])
        dur_index = {}
        for e in events:
            if e.get("type") == "Sortie" and e.get("duration", "-") != "-":
                dur_str = e["duration"]
                h = re.search(r"(\d+)h", dur_str)
                m = re.search(r"(\d+)m", dur_str)
                mins = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
                if mins > 0:
                    key = (str(e.get("deviceId","")), str(e.get("geofenceId","")),
                           (e.get("date") or "")[:16])
                    dur_index[key] = mins
        if dur_index:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("SELECT device_id, geofence_id, exit_time FROM interventions WHERE duration_min IS NULL")
                for row in cur.fetchall():
                    k = (str(row[0]), str(row[1]), (row[2] or "")[:16])
                    if k in dur_index:
                        cur.execute(
                            "UPDATE interventions SET duration_min=? WHERE device_id=? AND geofence_id=? AND exit_time=?",
                            (dur_index[k], row[0], row[1], row[2])
                        )
                conn.commit()
    except Exception:
        pass

    # ── Chargement DB ──
    dashboard._ensure_sous_parcelles_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT geofence_id, nom_parcelle, identifiant FROM parcelles")
        parcelle_map = {str(r["geofence_id"]): r["nom_parcelle"] or "" for r in cur.fetchall()}

        sous_parcelles_map = {k: v["nom"] for k, v in dashboard._get_sous_parcelles_info(conn).items()}

        cur.execute("""
            SELECT device_id, geofence_id, exit_time,
                   vehicle_name, tool_detected, intervention_type,
                   applied_area, duration_min, products, sous_parcelle_id
            FROM interventions ORDER BY exit_time ASC
        """)
        rows = cur.fetchall()

        cur.execute("SELECT name, type, dose, dose_homologuee, unit FROM catalog_products")
        catalog = {r["name"]: dict(r) for r in cur.fetchall()}

    # ── Listes déroulantes ──
    all_vehicles  = sorted({(r["vehicle_name"] or "").strip() for r in rows if r["vehicle_name"]})
    all_tools     = sorted({(r["tool_detected"] or "").strip() for r in rows if r["tool_detected"]})
    all_types     = sorted({(r["intervention_type"] or "").strip() for r in rows if r["intervention_type"]})

    def _label_for_row(r):
        base = parcelle_map.get(str(r["geofence_id"])) or f"Parcelle {r['geofence_id']}"
        sp_nom = sous_parcelles_map.get(r["sous_parcelle_id"]) if r["sous_parcelle_id"] else None
        return f"{base} — {sp_nom}" if sp_nom else base

    all_parcelles = sorted({_label_for_row(r) for r in rows})

    # ── Helpers ──
    def parse_area(v):
        try:
            return max(0.0, float(str(v).replace(",", ".").replace(" ha", "").strip()))
        except Exception:
            return 0.0

    def in_range(date_str):
        if not date_str:
            return True
        try:
            dt = datetime.strptime(date_str[:16], "%Y-%m-%dT%H:%M")
            if start_str and dt < datetime.strptime(start_str[:16], "%Y-%m-%dT%H:%M"):
                return False
            if end_str   and dt > datetime.strptime(end_str[:16],   "%Y-%m-%dT%H:%M"):
                return False
        except Exception:
            pass
        return True

    def acc(d, key, area, mins):
        if key not in d:
            d[key] = {"passages": 0, "surface_ha": 0.0, "minutes": 0, "n_with_dur": 0}
        d[key]["passages"]   += 1
        d[key]["surface_ha"] += area
        if mins:
            d[key]["minutes"]    += mins
            d[key]["n_with_dur"] += 1

    def fmt(d):
        out = []
        for k, v in sorted(d.items(), key=lambda x: -x[1]["surface_ha"]):
            h_tot, mn_tot = divmod(v["minutes"], 60)
            ha_h = round(v["surface_ha"] / (v["minutes"] / 60), 2) if v["minutes"] > 0 else None
            out.append({
                "label":    k,
                "passages": v["passages"],
                "surface":  round(v["surface_ha"], 2),
                "minutes":  v["minutes"],
                "duree":    f"{h_tot}h{mn_tot:02d}" if v["minutes"] > 0 else "—",
                "ha_h":     ha_h,
                "n_dur":    v["n_with_dur"],
            })
        return out

    # ── Aggrégations principales ──
    by_parcelle  = {}
    by_tracteur  = {}
    by_outil     = {}
    by_type      = {}
    by_mois      = {}
    combinaisons = {}
    timeline     = []

    # ── Suivi parcellaire ──
    # heatmap[parcelle][mois] = nb passages
    heatmap = {}
    # historique_parcelle[parcelle] = liste interventions triées
    hist_parcelle = {}
    # delai_parcelle[parcelle] = liste des délais entre passages (jours)
    delai_parcelle = {}

    # ── Produits & phyto ──
    # prod_usage[nom_produit] = {type, surface_ha, quantite_totale, passages, doses[]}
    prod_usage    = {}
    # prod_par_parcelle[parcelle][produit] = surface_ha
    prod_parcelle = {}

    fv = f_vehicle.strip().lower()
    fg = f_geo.strip().lower()
    ft = f_tool.strip().lower()
    fi = f_type.strip().lower()

    for row in rows:
        exit_time = (row["exit_time"] or "")
        if not in_range(exit_time):
            continue

        vehicle = (row["vehicle_name"]      or "Inconnu").strip()
        tool    = (row["tool_detected"]     or "—").strip()
        itype   = (row["intervention_type"] or "—").strip()
        area    = parse_area(row["applied_area"])
        mins    = row["duration_min"] or 0
        geo_id  = str(row["geofence_id"] or "")
        p_label = _label_for_row(row)

        if fv and fv not in vehicle.lower(): continue
        if fg and fg not in p_label.lower(): continue
        if ft and ft not in tool.lower():    continue
        if fi and fi not in itype.lower():   continue

        mois_key = ""
        try:
            mois_key = datetime.strptime(exit_time[:10], "%Y-%m-%d").strftime("%Y-%m")
        except Exception:
            pass

        acc(by_parcelle,  p_label,              area, mins)
        acc(by_tracteur,  vehicle,               area, mins)
        acc(by_outil,     tool,                  area, mins)
        acc(by_type,      itype,                 area, mins)
        acc(combinaisons, f"{vehicle} × {tool}", area, mins)
        if mois_key:
            acc(by_mois, mois_key, area, mins)

        timeline.append({
            "date": exit_time[:10], "parcelle": p_label,
            "tracteur": vehicle, "outil": tool, "type": itype,
            "surface": round(area, 2), "minutes": mins,
        })

        # ── Heatmap ──
        if p_label and mois_key:
            heatmap.setdefault(p_label, {})
            heatmap[p_label][mois_key] = heatmap[p_label].get(mois_key, 0) + 1

        # ── Historique parcelle ──
        hist_parcelle.setdefault(p_label, []).append({
            "date": exit_time[:10], "type": itype, "outil": tool,
            "tracteur": vehicle, "surface": round(area, 2), "minutes": mins,
        })

        # ── Produits ──
        try:
            prods = json.loads(row["products"]) if row["products"] else []
        except Exception:
            prods = []

        for p in prods:
            pname   = (p.get("name") or "").strip()
            ptype   = (p.get("type") or "—")
            dosage  = float(p.get("dosage") or 0)
            if not pname:
                continue
            cat_info = catalog.get(pname, {})
            dose_hom = cat_info.get("dose_homologuee") or 0
            unit     = cat_info.get("unit") or ""
            quantite = round(dosage * area, 3) if area > 0 else 0

            if pname not in prod_usage:
                prod_usage[pname] = {
                    "type": ptype, "unit": unit,
                    "surface_ha": 0.0, "quantite": 0.0,
                    "passages": 0, "doses": [],
                    "dose_homologuee": dose_hom,
                }
            prod_usage[pname]["surface_ha"] += area
            prod_usage[pname]["quantite"]   += quantite
            prod_usage[pname]["passages"]   += 1
            if dosage > 0:
                prod_usage[pname]["doses"].append(dosage)

            # prod par parcelle
            prod_parcelle.setdefault(p_label, {})
            prod_parcelle[p_label].setdefault(pname, 0.0)
            prod_parcelle[p_label][pname] += area

    timeline.sort(key=lambda x: x["date"])

    # ── Calcul délais entre passages ──
    for p_label, entries in hist_parcelle.items():
        entries.sort(key=lambda x: x["date"])
        delais = []
        for i in range(1, len(entries)):
            try:
                d1 = datetime.strptime(entries[i-1]["date"], "%Y-%m-%d")
                d2 = datetime.strptime(entries[i]["date"],   "%Y-%m-%d")
                delais.append((d2 - d1).days)
            except Exception:
                pass
        delai_parcelle[p_label] = {
            "min":   min(delais) if delais else None,
            "max":   max(delais) if delais else None,
            "moyen": round(sum(delais)/len(delais), 1) if delais else None,
            "nb":    len(delais),
        }

    # ── Formatage heatmap ──
    all_mois_keys = sorted({m for pm in heatmap.values() for m in pm})
    heatmap_out = []
    for p_label in sorted(heatmap.keys()):
        row_data = []
        for mk in all_mois_keys:
            row_data.append(heatmap[p_label].get(mk, 0))
        heatmap_out.append({"parcelle": p_label, "data": row_data})

    # ── Formatage produits ──
    prod_list = []
    for pname, v in sorted(prod_usage.items(), key=lambda x: -x[1]["surface_ha"]):
        dose_moy = round(sum(v["doses"]) / len(v["doses"]), 3) if v["doses"] else 0
        dose_hom = v["dose_homologuee"]
        ratio    = round(dose_moy / dose_hom * 100, 1) if dose_hom and dose_moy else None
        prod_list.append({
            "name":           pname,
            "type":           v["type"],
            "unit":           v["unit"],
            "surface_ha":     round(v["surface_ha"], 2),
            "quantite":       round(v["quantite"], 2),
            "passages":       v["passages"],
            "dose_moy":       dose_moy,
            "dose_homologuee":dose_hom,
            "ratio_dose":     ratio,
        })

    # ── prod par parcelle : top 3 produits par parcelle ──
    prod_parc_out = []
    for p_label in sorted(prod_parcelle.keys()):
        prods_sorted = sorted(prod_parcelle[p_label].items(), key=lambda x: -x[1])[:5]
        prod_parc_out.append({
            "parcelle": p_label,
            "produits": [{"name": n, "surface": round(s, 2)} for n, s in prods_sorted],
        })

    # ── Formatage historique parcelle ──
    hist_out = []
    for p_label in sorted(hist_parcelle.keys()):
        entries = hist_parcelle[p_label]
        total_s = round(sum(e["surface"] for e in entries), 2)
        total_m = sum(e["minutes"] for e in entries)
        h, mn   = divmod(total_m, 60)
        last    = entries[-1]["date"] if entries else ""
        delai   = delai_parcelle.get(p_label, {})
        hist_out.append({
            "parcelle":      p_label,
            "passages":      len(entries),
            "surface_total": total_s,
            "duree_total":   f"{h}h{mn:02d}" if total_m > 0 else "—",
            "derniere":      last,
            "delai_moy":     delai.get("moyen"),
            "delai_min":     delai.get("min"),
            "delai_max":     delai.get("max"),
            "interventions": entries,
        })

    total_mins    = sum(v["minutes"]    for v in by_tracteur.values())
    total_surface = round(sum(v["surface_ha"] for v in by_tracteur.values()), 2)
    total_pass    = sum(v["passages"]   for v in by_tracteur.values())
    global_ha_h   = round(total_surface / (total_mins / 60), 2) if total_mins > 0 else None

    return jsonify({
        "by_parcelle":   fmt(by_parcelle),
        "by_tracteur":   fmt(by_tracteur),
        "by_outil":      fmt(by_outil),
        "by_type":       fmt(by_type),
        "by_mois":       fmt(by_mois),
        "combinaisons":  fmt(combinaisons),
        "timeline":      timeline,
        "listes": {
            "vehicles":  all_vehicles,
            "tools":     all_tools,
            "types":     all_types,
            "parcelles": all_parcelles,
        },
        "totaux": {
            "passages": total_pass,
            "minutes":  total_mins,
            "surface":  total_surface,
            "ha_h":     global_ha_h,
        },
        "suivi_parcellaire": {
            "heatmap":      heatmap_out,
            "mois_labels":  all_mois_keys,
            "historique":   hist_out,
        },
        "phyto": {
            "produits":      prod_list,
            "par_parcelle":  prod_parc_out,
        },
    })

@analytique_bp.route("/export_pdf_analytique")
def export_pdf_analytique():
    try:
        DB_PATH='database.db'; start_str=request.args.get('start',''); end_str=request.args.get('end','')
        f_vehicle=request.args.get('vehicle',''); f_geo=request.args.get('geofence','')
        dashboard._ensure_sous_parcelles_table()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory=sqlite3.Row; cur=conn.cursor()
            cur.execute("SELECT geofence_id,nom_parcelle FROM parcelles")
            parcelle_map={str(r['geofence_id']):r['nom_parcelle'] or '' for r in cur.fetchall()}
            sous_parcelles_map={k: v["nom"] for k, v in dashboard._get_sous_parcelles_info(conn).items()}
            cur.execute("SELECT device_id,geofence_id,exit_time,vehicle_name,tool_detected,intervention_type,applied_area,duration_min,sous_parcelle_id FROM interventions ORDER BY exit_time ASC")
            rows=cur.fetchall()
            cur.execute("SELECT raison_sociale FROM exploitation WHERE id=1")
            row2=cur.fetchone(); exploitation=row2['raison_sociale'] if row2 else ''
        def pa(v):
            try: return max(0.0,float(str(v).replace(',','.').replace(' ha','').strip()))
            except Exception: return 0.0
        def ir(d):
            if not d: return True
            try:
                dt=datetime.strptime(d[:16],'%Y-%m-%dT%H:%M')
                if start_str and dt<datetime.strptime(start_str[:16],'%Y-%m-%dT%H:%M'): return False
                if end_str   and dt>datetime.strptime(end_str[:16],  '%Y-%m-%dT%H:%M'): return False
            except Exception: pass
            return True
        def acc(d,k,a,m):
            if k not in d: d[k]={'passages':0,'surface':0.0,'minutes':0}
            d[k]['passages']+=1; d[k]['surface']+=a; d[k]['minutes']+=m or 0
        def fd(m):
            if not m: return '—'
            h,mn=divmod(m,60); return f"{h}h{mn:02d}" if h else f"{mn}min"
        def fh(s,m):
            if m>0 and s>0: return f"{round(s/(m/60),2)} ha/h"
            return '—'
        def safe(t): return str(t or '').encode('latin-1','replace').decode('latin-1')
        by_p={}; by_t={}; by_o={}; by_ty={}; by_m={}; tl=[]
        for row in rows:
            et=(row['exit_time'] or '')
            if not ir(et): continue
            veh=(row['vehicle_name'] or 'Inconnu').strip(); tool=(row['tool_detected'] or '—').strip()
            ity=(row['intervention_type'] or '—').strip(); area=pa(row['applied_area']); mins=row['duration_min'] or 0
            geo=str(row['geofence_id'] or '')
            pl_base = parcelle_map.get(geo) or f"Parcelle {geo}"
            sp_nom = sous_parcelles_map.get(row['sous_parcelle_id']) if row['sous_parcelle_id'] else None
            pl = f"{pl_base} — {sp_nom}" if sp_nom else pl_base
            if f_vehicle and f_vehicle.lower() not in veh.lower(): continue
            if f_geo     and f_geo.lower()     not in pl.lower():  continue
            mk=''
            try: mk=datetime.strptime(et[:10],'%Y-%m-%d').strftime('%Y-%m')
            except Exception: pass
            acc(by_p,pl,area,mins); acc(by_t,veh,area,mins); acc(by_o,tool,area,mins)
            acc(by_ty,ity,area,mins)
            if mk: acc(by_m,mk,area,mins)
            tl.append({'date':et[:10],'parcelle':pl,'tracteur':veh,'outil':tool,'type':ity,'surface':round(area,2),'minutes':mins})
        tl.sort(key=lambda x:x['date'])
        pdf=FPDF(orientation='L',unit='mm',format='A4'); pdf.set_auto_page_break(auto=True,margin=15); pw=277
        def sh(title,r,g,b):
            pdf.set_fill_color(r,g,b); pdf.set_text_color(255,255,255); pdf.set_font('Arial','B',11)
            pdf.cell(0,8,safe(f"  {title}"),ln=1,fill=True); pdf.set_text_color(0,0,0); pdf.ln(1)
        def th(cols,widths):
            pdf.set_fill_color(37,99,235); pdf.set_text_color(255,255,255); pdf.set_font('Arial','B',9)
            for i,col in enumerate(cols): pdf.cell(widths[i],7,safe(col),border=1,align='C',fill=True)
            pdf.ln(); pdf.set_text_color(0,0,0)
        def tr(vals,widths,fill=False,bold=False):
            pdf.set_fill_color(235,240,255) if fill else pdf.set_fill_color(255,255,255)
            pdf.set_font('Arial','B' if bold else '',9)
            for i,val in enumerate(vals): pdf.cell(widths[i],6,safe(str(val)),border=1,fill=fill)
            pdf.ln()
        pdf.add_page()
        pdf.set_fill_color(37,99,235); pdf.rect(0,0,297,40,'F')
        pdf.set_font('Arial','B',22); pdf.set_text_color(255,255,255); pdf.set_y(10)
        pdf.cell(0,12,safe('Rapport Analytique'),align='C',ln=1)
        pdf.set_font('Arial','',13); pdf.cell(0,8,safe(exploitation),align='C',ln=1)
        pdf.set_text_color(0,0,0)
        pdf.add_page()
        cols=['Groupe','Passages','Surface (ha)','Durée','ha/h']; widths=[pw*0.35,pw*0.13,pw*0.18,pw*0.17,pw*0.17]
        for grp_data,grp_label in [(by_p,'Parcelle'),(by_t,'Tracteur'),(by_o,'Outil'),(by_ty,"Type d'intervention")]:
            sh(f'Par {grp_label}',37,99,235); th(cols,widths)
            ts=tm=tp=0
            for i,(k,v) in enumerate(sorted(grp_data.items(),key=lambda x:-x[1]['surface'])):
                tr([k,v['passages'],round(v['surface'],2),fd(v['minutes']),fh(v['surface'],v['minutes'])],widths,fill=i%2==0)
                ts+=v['surface']; tm+=v['minutes']; tp+=v['passages']
            tr(['TOTAL',tp,round(ts,2),fd(tm),fh(ts,tm)],widths,bold=True); pdf.ln(4)
        pdf.add_page(); sh('Évolution mensuelle',37,99,235)
        cols_m=['Mois','Passages','Surface (ha)','Durée','ha/h']; widths_m=[pw*0.25,pw*0.15,pw*0.20,pw*0.20,pw*0.20]
        th(cols_m,widths_m)
        for i,(k,v) in enumerate(sorted(by_m.items())):
            try:
                y,m2=k.split('-')
                from datetime import datetime as dtt; ml=dtt(int(y),int(m2),1).strftime('%B %Y')
            except Exception: ml=k
            tr([ml,v['passages'],round(v['surface'],2),fd(v['minutes']),fh(v['surface'],v['minutes'])],widths_m,fill=i%2==0)
        pdf.add_page(); sh('Chronologie',37,99,235)
        cols_t=['Date','Parcelle','Tracteur','Outil','Type','Surface','Durée','ha/h']
        widths_t=[pw*0.09,pw*0.17,pw*0.13,pw*0.13,pw*0.13,pw*0.10,pw*0.12,pw*0.13]
        th(cols_t,widths_t)
        for i,t in enumerate(tl):
            ds=t['date']
            try: y,m2,d=ds.split('-'); ds=f"{d}/{m2}/{y}"
            except Exception: pass
            tr([ds,t['parcelle'],t['tracteur'],t['outil'],t['type'],f"{t['surface']} ha",fd(t['minutes']),fh(t['surface'],t['minutes'])],widths_t,fill=i%2==0)
        pdf.set_y(-12); pdf.set_font('Arial','I',8)
        from datetime import datetime as dt3
        pdf.cell(0,5,safe(f"Imprimé le {dt3.now().strftime('%d/%m/%Y à %H:%M')}"),align='R')
        os.makedirs('exports',exist_ok=True); path=os.path.join('exports','analytique.pdf')
        pdf.output(path); return send_file(path,as_attachment=True,download_name='rapport_analytique.pdf')
    except Exception as e:
        return f"Erreur PDF analytique : {e}<br><pre>{__import__('traceback').format_exc()}</pre>",500
