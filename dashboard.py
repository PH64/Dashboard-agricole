#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import os
import time
import re
import csv
import json
import sqlite3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, jsonify, send_file, request, session as flask_session, redirect, url_for
from functools import wraps
import requests
import secrets
import threading
from openpyxl import Workbook
from fpdf import FPDF

# --- IMPORTATION DU MODULE CARNET DE PLAINE ---
from interventions import interventions_bp, init_db

DASHBOARD_VERSION = "12.3"

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # Clé de session aléatoire

# ================= AUTHENTIFICATION =================
LOGIN_USER     = "admin"       # ← Changer ici
LOGIN_PASSWORD = "changeme"  # ← Changer ici
try:
    with open("password_override.txt", "r") as f:
        _saved_pw = f.read().strip()
        if _saved_pw:
            LOGIN_PASSWORD = _saved_pw
except FileNotFoundError:
    pass
SESSION_HOURS  = 8             # Durée de session en heures
MAX_LOGIN_ATTEMPTS = 5         # Tentatives max avant blocage 15 min
LOGIN_ATTEMPTS = {}            # {ip: [timestamps]}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get('logged_in'):
            # Routes API : renvoyer JSON 401 au lieu d'un redirect HTML
            if request.path.startswith('/api/'):
                return jsonify({"error": "Non authentifié", "redirect": "/login"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# --- ENREGISTREMENT DU BLUEPRINT DES INTERVENTIONS ---
app.register_blueprint(interventions_bp)

# ================= CONFIGURATION =================
# ── Configuration Traccar (chargée depuis config.json si disponible) ──
def _load_traccar_config():
    import json, os
    defaults = {
        "url":      "http://votre-traccar:8082/api",
        "user":     "votre@email.com",
        "password": "",
        "days_back": 30,
        "cache_duration": 60,
    }
    try:
        if os.path.exists("config.json"):
            with open("config.json", "r") as f:
                data = json.load(f)
                defaults.update(data.get("traccar", {}))
    except Exception:
        pass
    return defaults

_tcfg = _load_traccar_config()
TRACCAR_URL      = _tcfg["url"]
TRACCAR_USER     = _tcfg["user"]
TRACCAR_PASSWORD = _tcfg["password"]

DAYS_BACK      = _tcfg.get("days_back", 30)
CACHE_DURATION = _tcfg.get("cache_duration", 60)

session = requests.Session()
session.auth = (TRACCAR_USER, TRACCAR_PASSWORD)
HEADERS = {"Accept": "application/json"}

_cached_data = None
_last_cache_time = 0

# ================= SAFE API GET =================
def safe_get(url):
    try:
        r = session.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception:
        return []

# ================= FORMATAGE DATE =================
def format_date_fr(dt_str):
    if not dt_str: return ""
    try:
        dt = datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d/%m/%Y %Hh%M")
    except Exception:
        return dt_str

# ================= CORE EXTRACTION =================
def get_devices():
    data = safe_get(f"{TRACCAR_URL}/devices")
    return {d["id"]: d for d in data if "id" in d} if isinstance(data, list) else {}

def get_geofences():
    data = safe_get(f"{TRACCAR_URL}/geofences")
    return {g["id"]: g for g in data if "id" in g} if isinstance(data, list) else {}

def fetch_positions_for_device(args):
    d_id, start_str, end_str = args
    url = f"{TRACCAR_URL}/reports/route?deviceId={d_id}&from={start_str}&to={end_str}"
    data = safe_get(url)
    if isinstance(data, list):
        for p in data:
            p["deviceId"] = d_id
        return data
    return []

def fetch_events_for_device(args):
    d_id, start_str, end_str = args
    url = f"{TRACCAR_URL}/reports/events?deviceId={d_id}&from={start_str}&to={end_str}"
    data = safe_get(url)
    if isinstance(data, list):
        return [e for e in data if "geo" in str(e.get("type", "")).lower() or e.get("geofenceId")]
    return []

def get_data_parallel(device_ids):
    end = datetime.utcnow()
    start = end - timedelta(days=DAYS_BACK)
    start_str = start.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end.strftime('%Y-%m-%dT%H:%M:%SZ')
    
    tasks = [(d_id, start_str, end_str) for d_id in device_ids]
    all_positions = []
    all_events = []
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        pos_results = executor.map(fetch_positions_for_device, tasks)
        evt_results = executor.map(fetch_events_for_device, tasks)
        
        for res in pos_results: all_positions.extend(res)
        for res in evt_results: all_events.extend(res)
            
    return sorted(all_positions, key=lambda x: x.get("fixTime", "")), all_events

def build_positions_index(positions):
    index = {}
    for p in positions:
        d_id = p.get("deviceId")
        if d_id: index.setdefault(d_id, []).append(p)
    return index

def find_position(device_id, event_time, positions_index):
    plist = positions_index.get(device_id, [])
    if not plist: return {}
    try:
        et = datetime.strptime(event_time[:19], "%Y-%m-%dT%H:%M:%S")
    except: return {}
    best, best_diff = {}, float("inf")
    for p in plist:
        try:
            pt = datetime.strptime(p.get("fixTime","")[:19], "%Y-%m-%dT%H:%M:%S")
            diff = abs((pt - et).total_seconds())
            if diff < best_diff:
                best, best_diff = p, diff
        except: continue
    return best if best_diff <= 600 else {}

def build_last_positions(positions):
    last = {}
    for p in positions:
        d = p.get("deviceId")
        if not d: continue
        if d not in last or p.get("fixTime", "") > last[d].get("fixTime", ""):
            last[d] = p
    return last

# ================= CACHE CONTROL & CALCUL DURÉES =================
def build_data():
    global _cached_data, _last_cache_time
    current_time = time.time()

    if _cached_data and (current_time - _last_cache_time < CACHE_DURATION):
        return _cached_data

    devices = get_devices()
    geofences = get_geofences()
    
    if not devices:
        return {"events": [], "positions": [], "geofences": {}}

    positions, events = get_data_parallel(list(devices.keys()))
    positions_index = build_positions_index(positions)
    last_positions = build_last_positions(positions)

    raw_events = []
    for e in events:
        device_id = e.get("deviceId")
        geo_id = e.get("geofenceId")
        pos = find_position(device_id, e.get("eventTime"), positions_index)
        attrs = pos.get("attributes") or {}
        etype = "Entrée" if "enter" in str(e.get("type","")).lower() else "Sortie"

        w_val = str(attrs.get("workingWidth", attrs.get("width", ""))).strip()
        width_str = ""
        if w_val and w_val != "None":
            if "m" in w_val.lower():
                width_str = w_val
            else:
                width_str = f"{w_val} m"

        # Capturer appliedArea de manière indépendante de field
        applied_area_val = attrs.get("appliedArea", "")

        raw_events.append({
            "deviceId": device_id,
            "geofenceId": geo_id,
            "vehicle": devices.get(device_id, {}).get("name", f"Véhicule {device_id}"),
            "geofence": geofences.get(geo_id, {}).get("name", f"Parcelle {geo_id}"),
            "type": etype,
            "date": e.get("eventTime"),
            "date_fr": format_date_fr(e.get("eventTime")),
            "field": attrs.get("field", ""),
            "appliedArea": str(applied_area_val).strip() if applied_area_val is not None else "",
            "tool": attrs.get("tool", ""),
            "width": width_str,
            "lat": pos.get("latitude", ""),
            "lon": pos.get("longitude", ""),
        })

    raw_events.sort(key=lambda x: x["date"])
    active_inputs = {} 
    
    for e in raw_events:
        key = (e["deviceId"], e["geofenceId"])
        if e["type"] == "Entrée":
            active_inputs[key] = e["date"]
            e["duration"] = "-"
        elif e["type"] == "Sortie":
            if key in active_inputs:
                try:
                    t_in = datetime.strptime(active_inputs[key][:19], "%Y-%m-%dT%H:%M:%S")
                    t_out = datetime.strptime(e["date"][:19], "%Y-%m-%dT%H:%M:%S")
                    diff = t_out - t_in
                    hours, remainder = divmod(int(diff.total_seconds()), 3600)
                    minutes, _ = divmod(remainder, 60)
                    e["duration"] = f"{hours}h{minutes:02d}m" if hours > 0 else f"{minutes}m"
                    del active_inputs[key]
                except Exception:
                    e["duration"] = "-"
            else:
                e["duration"] = "-"

    _cached_data = {
        "events": raw_events,
        "positions": [
            {
                "vehicle": devices.get(p.get("deviceId"), {}).get("name", "Inconnu"),
                "lat": p.get("latitude"),
                "lon": p.get("longitude"),
                "date": p.get("fixTime")
            }
            for p in last_positions.values()
        ],
        "geofences": geofences
    }
    _last_cache_time = current_time
    print(f"✅ Cache v7.14 mis à jour (Surfaces distinctes : field & appliedArea).")
    return _cached_data

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.remote_addr
    now = time.time()

    # Nettoyer les anciennes tentatives (> 15 min)
    LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 900]

    if len(LOGIN_ATTEMPTS.get(ip, [])) >= MAX_LOGIN_ATTEMPTS:
        wait_min = int((900 - (now - LOGIN_ATTEMPTS[ip][0])) / 60) + 1
        error = f"Trop de tentatives. Réessayez dans {wait_min} min."
        return render_template("login.html", error=error)

    if request.method == "POST":
        if request.form.get("username") == LOGIN_USER and request.form.get("password") == LOGIN_PASSWORD:
            LOGIN_ATTEMPTS.pop(ip, None)
            flask_session.permanent = True
            app.permanent_session_lifetime = timedelta(hours=SESSION_HOURS)
            flask_session["logged_in"] = True
            return redirect(url_for("index"))
        LOGIN_ATTEMPTS.setdefault(ip, []).append(now)
        remaining = MAX_LOGIN_ATTEMPTS - len(LOGIN_ATTEMPTS[ip])
        error = f"Identifiants incorrects. ({remaining} tentative(s) restante(s))" if remaining > 0 else "Trop de tentatives. Réessayez dans 15 min."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    flask_session.clear()
    return redirect(url_for("login"))

@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    global LOGIN_PASSWORD
    error = None
    success = None
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if current != LOGIN_PASSWORD:
            error = "Mot de passe actuel incorrect."
        elif len(new_pw) < 6:
            error = "Le nouveau mot de passe doit faire au moins 6 caractères."
        elif new_pw != confirm:
            error = "Les deux mots de passe ne correspondent pas."
        else:
            LOGIN_PASSWORD = new_pw
            # Persister dans un fichier pour survivre au redémarrage
            try:
                with open("password_override.txt", "w") as f:
                    f.write(new_pw)
            except Exception:
                pass
            success = "Mot de passe mis à jour avec succès."
    return render_template("change_password.html", error=error, success=success)

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/fertilisation")
@login_required
def fertilisation():
    import os
    for p in [os.path.join(app.template_folder or "templates", "fertilisation.html"),
              os.path.join(os.path.dirname(__file__), "fertilisation.html"),
              "fertilisation.html"]:
        if os.path.isfile(p):
            return send_file(p)
    return render_template("fertilisation.html")

@app.route("/analytique")
@login_required
def analytique():
    import os
    for p in [os.path.join(app.template_folder or "templates", "analytique.html"),
              os.path.join(os.path.dirname(__file__), "analytique.html"),
              "analytique.html"]:
        if os.path.isfile(p):
            return send_file(p)
    return render_template("analytique.html")

@app.route("/aide")
@login_required
def aide():
    import os
    for p in [os.path.join(app.template_folder or "templates", "Notice.html"),
              os.path.join(os.path.dirname(__file__), "Notice.html"),
              "Notice.html"]:
        if os.path.isfile(p):
            return send_file(p)
    return render_template("Notice.html")

def apply_filters(events, vehicle, geofence, start, end):
    def parse_dt(s):
        if not s: return None
        try: return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
        except:
            try: return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            except: return None

    filtered = events
    if start:
        s = parse_dt(start)
        if s: filtered = [e for e in filtered if parse_dt(e.get("date")) and parse_dt(e.get("date")) >= s]
    if end:
        e_dt = parse_dt(end)
        if e_dt: filtered = [e for e in filtered if parse_dt(e.get("date")) and parse_dt(e.get("date")) <= e_dt]
    if vehicle:
        filtered = [e for e in filtered if e["vehicle"] == vehicle]
    if geofence:
        filtered = [e for e in filtered if e["geofence"] == geofence]
    return filtered

STATUT_PAR_TYPE_INTERVENTION = {
    'Semis': 'semis',
    'Pulvérisation': 'traite',
    'Épandage': 'traite',
    'Récolte': 'recolte',
    'Labour': 'prepare',
    'Hersage': 'prepare',
    'Déchaumage': 'prepare',
    'Broyage': 'prepare',
    'Travail du sol': 'prepare',
}


@app.route("/api/parcelles/refresh_statuts", methods=["POST"])
@login_required
def refresh_statuts():
    """
    Recalcule le statut automatique de chaque parcelle à partir de sa
    dernière intervention enregistrée. Ne touche pas aux parcelles en
    mode manuel (statut_auto = 0).
    """
    DB_PATH = 'database.db'
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Dernière intervention par parcelle (la plus récente exit_time)
        cur.execute("""
            SELECT geofence_id, intervention_type, exit_time
            FROM interventions
            ORDER BY exit_time ASC
        """)
        derniere_par_parcelle = {}
        for row in cur.fetchall():
            derniere_par_parcelle[row['geofence_id']] = row['intervention_type']

        # Parcelles en mode automatique uniquement
        cur.execute("SELECT geofence_id FROM parcelles WHERE statut_auto = 1 OR statut_auto IS NULL")
        parcelles_auto = [row[0] for row in cur.fetchall()]

        updated = 0
        for geo_id in parcelles_auto:
            type_interv = derniere_par_parcelle.get(geo_id)
            nouveau_statut = STATUT_PAR_TYPE_INTERVENTION.get(type_interv, 'attente')
            cur.execute(
                "UPDATE parcelles SET statut = ? WHERE geofence_id = ? AND statut_auto = 1",
                (nouveau_statut, geo_id)
            )
            updated += cur.rowcount

        conn.commit()

    return jsonify({"status": "success", "updated": updated})


def resolve_geo_name(geofences, geo_id):
    """geofences peut être indexé par int ou str selon la source : on tente les deux clés."""
    info = geofences.get(geo_id)
    if info is None:
        info = geofences.get(str(geo_id))
    if info is None:
        try:
            info = geofences.get(int(geo_id))
        except (ValueError, TypeError):
            info = None
    if isinstance(info, dict) and info.get('name'):
        return info['name']
    return f"Parcelle {geo_id}"


@app.route("/api/today_summary")
@login_required
def today_summary():
    """
    Résumé condensé pour la page d'accueil : interventions du jour, nombre d'alertes
    DAR actives, parcelles nécessitant une attention (statut 'attente' ou 'préparé'
    depuis longtemps), et position GPS du premier véhicule pour la météo locale.
    """
    DB_PATH = 'database.db'
    today_str = datetime.now().strftime("%Y-%m-%d")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Interventions du jour (saisies dans le carnet)
        cur.execute("""
            SELECT geofence_id, intervention_type, exit_time, products, applied_area
            FROM interventions
            WHERE exit_time LIKE ?
            ORDER BY exit_time DESC
        """, (f"{today_str}%",))
        interventions_today = [dict(r) for r in cur.fetchall()]

        # Parcelles en attente ou préparées, qui mériteraient une action
        cur.execute("""
            SELECT geofence_id, identifiant, nom_parcelle, statut
            FROM parcelles
            WHERE statut IN ('attente', 'prepare')
        """)
        parcelles_attention = [dict(r) for r in cur.fetchall()]

    # Réutiliser la logique d'alertes DAR déjà en place
    alertes_response = alertes_dar()
    try:
        alertes_data = alertes_response.get_json()
    except Exception:
        alertes_data = {"alertes": [], "nb_alertes": 0}

    # Position GPS du premier véhicule actif, pour que le front affiche la météo locale
    raw = build_data()
    first_position = None
    for p in raw.get("positions", []):
        if p.get("lat") and p.get("lon"):
            first_position = {"lat": p["lat"], "lon": p["lon"]}
            break

    geofences = raw.get("geofences", {})
    for p in parcelles_attention:
        p['nom_traccar'] = resolve_geo_name(geofences, p['geofence_id'])

    for interv in interventions_today:
        interv['nom_parcelle'] = resolve_geo_name(geofences, interv['geofence_id'])

    return jsonify({
        "date": today_str,
        "interventions_today": interventions_today,
        "nb_interventions_today": len(interventions_today),
        "parcelles_attention": parcelles_attention,
        "nb_parcelles_attention": len(parcelles_attention),
        "alertes_dar": alertes_data.get("alertes", []),
        "nb_alertes_dar": alertes_data.get("nb_alertes", 0),
        "first_position": first_position,
    })


@app.route("/api/alertes_dar")
@login_required
def alertes_dar():
    """
    Vérifie la cohérence DAR/récolte : pour chaque récolte enregistrée, vérifie si un
    produit phyto utilisé avant cette récolte sur la même parcelle avait encore un
    DAR (Délai Avant Récolte) actif au moment de la récolte. Remonte les anomalies.
    """
    DB_PATH = 'database.db'
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT geofence_id, intervention_type, exit_time, products
            FROM interventions
            ORDER BY exit_time ASC
        """)
        all_interventions = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, type, dar FROM catalog_products WHERE type = 'phyto' AND dar > 0")
        catalog_dar = {r['name'].strip(): r['dar'] for r in cur.fetchall()}

    raw = build_data()
    geofences = raw.get("geofences", {})

    alertes = []
    recoltes = [i for i in all_interventions if i['intervention_type'] == 'Récolte']

    for recolte in recoltes:
        try:
            dt_recolte = datetime.strptime(recolte['exit_time'][:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            try:
                dt_recolte = datetime.strptime(recolte['exit_time'][:10], "%Y-%m-%d")
            except Exception:
                continue

        # Chercher les interventions de pulvérisation AVANT cette récolte, sur la même parcelle
        for interv in all_interventions:
            if interv['geofence_id'] != recolte['geofence_id']:
                continue
            if interv['intervention_type'] not in ('Pulvérisation', 'Épandage'):
                continue
            try:
                dt_traitement = datetime.strptime(interv['exit_time'][:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                try:
                    dt_traitement = datetime.strptime(interv['exit_time'][:10], "%Y-%m-%d")
                except Exception:
                    continue

            if dt_traitement >= dt_recolte:
                continue  # traitement après la récolte, pas pertinent ici

            try:
                products = json.loads(interv.get('products') or '[]')
            except Exception:
                products = []

            for prod in products:
                prod_name = (prod.get('name') or '').strip()
                dar_jours = catalog_dar.get(prod_name)
                if not dar_jours:
                    continue
                jours_ecoules = (dt_recolte - dt_traitement).days
                if jours_ecoules < dar_jours:
                    alertes.append({
                        'geofence_id': recolte['geofence_id'],
                        'nom_parcelle': resolve_geo_name(geofences, recolte['geofence_id']),
                        'produit': prod_name,
                        'date_traitement': dt_traitement.strftime("%d/%m/%Y"),
                        'date_recolte': dt_recolte.strftime("%d/%m/%Y"),
                        'dar_requis_jours': dar_jours,
                        'jours_ecoules': jours_ecoules,
                        'jours_manquants': dar_jours - jours_ecoules,
                    })

    return jsonify({"alertes": alertes, "nb_alertes": len(alertes)})


@app.route("/data")
@login_required
def data():
    vehicle = request.args.get("vehicle","")
    geofence = request.args.get("geofence","")
    start = request.args.get("start")
    end = request.args.get("end")

    # Calculer dynamiquement DAYS_BACK selon la période demandée
    global DAYS_BACK, _cached_data, _last_cache_time
    if start:
        try:
            dt_start = datetime.strptime(start[:16], "%Y-%m-%dT%H:%M")
            days_needed = (datetime.utcnow() - dt_start).days + 1
            if days_needed != DAYS_BACK:
                DAYS_BACK = max(1, days_needed)
                _cached_data = None  # Invalider le cache
                _last_cache_time = 0
        except Exception:
            pass

    raw = build_data()
    events = apply_filters(raw["events"], vehicle, geofence, start, end)
    return jsonify({"events": events, "positions": raw["positions"], "geofences": raw["geofences"]})

@app.route("/export_excel")
@login_required
def export_excel():
    raw = build_data()
    event_data = apply_filters(raw["events"], request.args.get("vehicle",""), request.args.get("geofence",""), request.args.get("start"), request.args.get("end"))
    wb = Workbook()
    ws = wb.active
    ws.append(["Vehicle","Geofence","Type","Date","Duration","Surf. Parcelle","Surf. Travaillee","Tool","Width"])
    for d in event_data:
        ws.append([d["vehicle"], d["geofence"], d["type"], d["date_fr"], d.get("duration","-"), d.get("field","-"), d.get("appliedArea","-"), d["tool"], d["width"]])
    file = io.BytesIO()
    wb.save(file)
    file.seek(0)
    return send_file(file, as_attachment=True, download_name="traccar_v7.14.xlsx")

@app.route("/export_pdf")
@login_required
def export_pdf():
    raw = build_data()
    event_data = apply_filters(raw["events"], request.args.get("vehicle",""), request.args.get("geofence",""), request.args.get("start"), request.args.get("end"))
    event_data.sort(key=lambda x: x["geofence"])
    
    geo_totals = {}
    for d in event_data:
        if d["type"] == "Sortie" and d.get("duration") and d["duration"] != "-":
            minutes = 0
            match_h = re.search(r'(\d+)h', d["duration"])
            match_m = re.search(r'(\d+)m', d["duration"])
            if match_h: minutes += int(match_h.group(1)) * 60
            if match_m: minutes += int(match_m.group(1))
            geo_totals[d["geofence"]] = geo_totals.get(d["geofence"], 0) + minutes

    pdf = FPDF(orientation="L")
    pdf.add_page()
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Rapport Chantiers Traccar - v7.14", ln=1, align="C")
    
    headers = ["Parcelle", "Surf. Parc", "Surf. Trav", "Date", "Type", "Durée", "Véhicule", "Outil", "Largeur"]
    page_width = pdf.w - 2 * pdf.l_margin
    widths = [
        page_width * 0.14, 
        page_width * 0.08, 
        page_width * 0.08, 
        page_width * 0.14, 
        page_width * 0.07, 
        page_width * 0.09, 
        page_width * 0.14, 
        page_width * 0.18, 
        page_width * 0.08  
    ]
    
    pdf.set_font("Arial", "B", 9)
    for i, h in enumerate(headers):
        pdf.cell(widths[i], 10, h.encode('latin-1', 'replace').decode('latin-1'), 1, align="C")
    pdf.ln()
    
    pdf.set_font("Arial", "", 9)
    current_geo = None
    for d in event_data:
        if d["geofence"] != current_geo:
            current_geo = d["geofence"]
            pdf.set_font("Arial", "B", 10)
            pdf.set_fill_color(220, 220, 220)
            
            total_min = geo_totals.get(current_geo, 0)
            if total_min > 0:
                h = total_min // 60
                m = total_min % 60
                total_str = f"Temps total : {h}h{m:02d}m" if h > 0 else f"Temps total : {m}m"
            else:
                total_str = "Temps total : 0m"

            title_txt = f" Parcelle : {current_geo}"
            pdf.cell(page_width * 0.6, 8, title_txt.encode('latin-1', 'replace').decode('latin-1'), 1, 0, fill=True)
            pdf.cell(page_width * 0.4, 8, f"{total_str} ", 1, 1, align="R", fill=True)
            pdf.set_font("Arial", "", 9)
            
        pdf.set_fill_color(*(212, 247, 212) if d["type"] == "Entrée" else (255, 214, 214))
        
        row = [
            d["geofence"], 
            d.get("field", "-"), 
            d.get("appliedArea", "-"), 
            d["date_fr"], 
            d["type"], 
            d.get("duration","-"), 
            d["vehicle"], 
            d["tool"],
            d["width"]
        ]
        for i, v in enumerate(row):
            val_txt = str(v if v else "-")
            pdf.cell(widths[i], 8, val_txt.encode('latin-1', 'replace').decode('latin-1'), 1, fill=True)
        pdf.ln()
        
    os.makedirs("exports", exist_ok=True)
    path = os.path.join("exports", "report.pdf")
    pdf.output(path)
    return send_file(path, as_attachment=True)

@app.route("/export_phyto")
@login_required
def export_phyto():
    """Export XML registre phyto/semis."""
    DB_PATH = 'database.db'

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT device_id, geofence_id, exit_time,
                   intervention_type, products, applied_area, meteo
            FROM interventions
            WHERE intervention_type IN ('Pulvérisation', 'Semis')
            ORDER BY exit_time DESC
        """)
        interventions = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, type, amm, unit, culture, bbch, target, bio FROM catalog_products")
        catalog = {r['name']: dict(r) for r in cur.fetchall()}

        cur.execute("SELECT nom, code_oepp FROM cultures")
        cultures_oepp = {r['nom'].strip().lower(): r['code_oepp'] for r in cur.fetchall()}

        cur.execute("SELECT geofence_id, identifiant, surface_ha FROM parcelles")
        parcelles_rows = cur.fetchall()
        parcelles = {str(r['geofence_id']): r['identifiant'] for r in parcelles_rows}
        parcelles_surface = {int(r['geofence_id']): r['surface_ha'] for r in parcelles_rows if r['surface_ha']}

        cur.execute("SELECT siret, raison_sociale, applicateur, certiphyto, materiel, num_controle, date_controle FROM exploitation WHERE id = 1")
        row = cur.fetchone()
        siret = row['siret'] if row else ''
        raison_sociale = row['raison_sociale'] if row else ''
        applicateur = row['applicateur'] if row else ''
        certiphyto = row['certiphyto'] if row else ''
        materiel = row['materiel'] if row else ''
        num_controle = row['num_controle'] if row else ''
        date_controle = row['date_controle'] if row else ''

    raw = build_data()
    gps_index = {}
    for e in raw["events"]:
        if e["type"] == "Sortie":
            key = (str(e["deviceId"]), str(e["geofenceId"]), e["date"][:19])
            gps_index[key] = (e.get("lat", ""), e.get("lon", ""))
    geofences = raw.get("geofences", {})

    def esc(s):
        return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")

    # Le registre phyto ne doit contenir QUE les produits phytosanitaires et semences,
    # jamais les engrais (même si saisis dans une intervention de type Pulvérisation/Épandage)
    TYPES_AUTORISES_REGISTRE = {'phyto', 'semence'}

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<registre_phytosanitaire>',
             '  <exploitation>',
             f'    <siret>{esc(siret)}</siret>',
             f'    <raison_sociale>{esc(raison_sociale)}</raison_sociale>',
             f'    <applicateur>{esc(applicateur)}</applicateur>',
             f'    <numero_certiphyto>{esc(certiphyto)}</numero_certiphyto>',
             '  </exploitation>',
             '  <materiel_pulverisation>',
             f'    <nom>{esc(materiel)}</nom>',
             f'    <numero_controle>{esc(num_controle)}</numero_controle>',
             f'    <date_dernier_controle>{esc(date_controle)}</date_dernier_controle>',
             '  </materiel_pulverisation>',
             '  <interventions>']

    for interv in interventions:
        geo_id_str = str(interv['geofence_id'])
        geo_name = geofences.get(geo_id_str, {}).get('name', f"Parcelle {interv['geofence_id']}")
        id_parcelle = parcelles.get(geo_id_str, '')
        gps_key = (str(interv['device_id']), geo_id_str, interv['exit_time'][:19])
        lat, lon = gps_index.get(gps_key, ("", ""))
        try:
            date_only = datetime.strptime(interv['exit_time'][:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            date_only = interv['exit_time'][:10]
        geo_id_int = int(interv.get('geofence_id', 0) or 0)
        surf_cadastrale_exp = parcelles_surface.get(geo_id_int)
        if surf_cadastrale_exp:
            surf = f"{surf_cadastrale_exp} ha"
        else:
            surf = (str(interv['applied_area']) + ' ha') if interv.get('applied_area') is not None else ''
        try:
            products = json.loads(interv['products']) if interv['products'] else []
        except Exception:
            products = []

        # Parser météo
        meteo = {}
        try:
            meteo = json.loads(interv.get('meteo') or '{}') or {}
        except Exception:
            meteo = {}

        # Construire la liste des produits éligibles AVANT toute écriture XML.
        # Le registre phyto exclut les engrais : seuls phyto et semences y figurent.
        produits_xml = []
        for prod in products:
            prod_name = prod.get('name', '')
            cat = catalog.get(prod_name, {})
            if cat.get('type') not in TYPES_AUTORISES_REGISTRE:
                continue
            prod_dose = prod.get('dosage', '')
            unit = cat.get('unit', '')
            dose_display = f"{prod_dose} {unit}/ha".strip() if prod_dose else ''
            produits_xml += [
                '        <produit>',
                f'          <nom>{esc(prod_name)}</nom>',
                f'          <numero_amm>{esc(cat.get("amm",""))}</numero_amm>',
                f'          <dose>{esc(dose_display)}</dose>',
                f'          <cible>{esc(cat.get("target",""))}</cible>',
                f'          <code_culture>{esc(cultures_oepp.get((cat.get("culture") or "").strip().lower(), "") or cat.get("culture",""))}</code_culture>',
                f'          <stade_bbch>{esc(cat.get("bbch",""))}</stade_bbch>',
                f'          <bio>{"Oui" if cat.get("bio") else "Non"}</bio>',
                '        </produit>',
            ]

        # Si aucun produit éligible (intervention 100% engrais), on saute entièrement
        # cette intervention : rien n'est ajouté à lines.
        if not produits_xml:
            continue

        lines += [
            '    <intervention>',
            f'      <date>{esc(date_only)}</date>',
            f'      <type>{esc(interv["intervention_type"])}</type>',
            f'      <applicateur>{esc(applicateur)}</applicateur>',
            f'      <numero_certiphyto>{esc(certiphyto)}</numero_certiphyto>',
            '      <meteo>',
            f'        <conditions>{esc(meteo.get("conditions",""))}</conditions>',
            f'        <temperature unite="°C">{esc(meteo.get("temperature",""))}</temperature>',
            f'        <vent unite="km/h">{esc(meteo.get("vent",""))}</vent>',
            f'        <pluie unite="mm">{esc(meteo.get("pluie",""))}</pluie>',
            '      </meteo>',
            '      <parcelle>',
            f'        <identifiant>{esc(id_parcelle)}</identifiant>',
            f'        <nom>{esc(id_parcelle if id_parcelle else geo_name)}</nom>',
            f'        <surface_travaillee unite="ha">{esc(surf)}</surface_travaillee>',
            f'        <gps_lat>{esc(lat)}</gps_lat>',
            f'        <gps_lon>{esc(lon)}</gps_lon>',
            '      </parcelle>',
            '      <produits>',
        ]
        lines += produits_xml
        lines += ['      </produits>', '    </intervention>']

    lines += ['  </interventions>', '</registre_phytosanitaire>']

    xml_content = "\n".join(lines)
    return send_file(
        io.BytesIO(xml_content.encode('utf-8')),
        as_attachment=True,
        download_name="registre_phyto.xml",
        mimetype='application/xml'
    )


@app.route("/api/export_config")
@login_required
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
    config['_export_version'] = DASHBOARD_VERSION

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


@app.route("/api/import_config", methods=["POST"])
@login_required
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


@app.route("/api/system_status")
@login_required
def system_status():
    """État du système : BDD, sauvegardes, dernière synchro Traccar."""
    DB_PATH = 'database.db'
    status = {'dashboard_version': DASHBOARD_VERSION}

    # Taille et dernière modif de la BDD
    if os.path.exists(DB_PATH):
        status['db_size_kb'] = round(os.path.getsize(DB_PATH) / 1024, 1)
        status['db_last_modified'] = datetime.fromtimestamp(os.path.getmtime(DB_PATH)).strftime("%d/%m/%Y %Hh%M")
    else:
        status['db_size_kb'] = 0
        status['db_last_modified'] = 'N/A'

    # Nombre d'interventions et de produits en BDD
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM interventions")
            status['nb_interventions'] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM catalog_products")
            status['nb_produits'] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM parcelles WHERE identifiant != ''")
            status['nb_parcelles_identifiees'] = cur.fetchone()[0]
    except Exception as e:
        status['db_error'] = str(e)

    # Liste des sauvegardes disponibles
    backup_dir = "backups"
    backups = []
    if os.path.exists(backup_dir):
        for fname in sorted(os.listdir(backup_dir), reverse=True):
            fpath = os.path.join(backup_dir, fname)
            if os.path.isfile(fpath):
                backups.append({
                    'name': fname,
                    'size_kb': round(os.path.getsize(fpath) / 1024, 1),
                    'date': datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%d/%m/%Y %Hh%M")
                })
    status['backups'] = backups
    status['backups_total_kb'] = round(sum(b['size_kb'] for b in backups), 1)

    # Dernière synchro Traccar (basée sur le cache)
    global _last_cache_time
    if _last_cache_time:
        status['last_traccar_sync'] = datetime.fromtimestamp(_last_cache_time).strftime("%d/%m/%Y %Hh%M")
        status['cache_age_seconds'] = round(time.time() - _last_cache_time)
    else:
        status['last_traccar_sync'] = 'Jamais'
        status['cache_age_seconds'] = None

    # Test de connexion Traccar en direct
    try:
        test = safe_get(f"{TRACCAR_URL}/devices")
        status['traccar_reachable'] = bool(test)
    except Exception:
        status['traccar_reachable'] = False

    status['traccar_url']       = TRACCAR_URL
    status['traccar_user']      = TRACCAR_USER
    status['traccar_days_back'] = DAYS_BACK
    status['traccar_cache']     = CACHE_DURATION

    return jsonify(status)


def backup_database():
    """Sauvegarde quotidienne de database.db dans un dossier daté."""
    import shutil
    try:
        db_path = "database.db"
        if not os.path.exists(db_path):
            return
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        backup_path = os.path.join(backup_dir, f"database_{today}.db")
        if not os.path.exists(backup_path):
            shutil.copy2(db_path, backup_path)
            print(f"✅ Sauvegarde créée : {backup_path}")
        cutoff = datetime.now() - timedelta(days=30)
        for fname in os.listdir(backup_dir):
            fpath = os.path.join(backup_dir, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde BDD: {e}")


def backup_scheduler():
    """Thread qui déclenche une sauvegarde toutes les 24h."""
    while True:
        backup_database()
        time.sleep(86400)



@app.route("/export_phyto_excel")
@login_required
def export_phyto_excel():
    """Export Excel registre phyto/semis — équivalent lisible du XML."""
    DB_PATH = 'database.db'
    TYPES_AUTORISES_REGISTRE = {'phyto', 'semence'}

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT device_id, geofence_id, exit_time,
                   intervention_type, products, applied_area, meteo
            FROM interventions
            WHERE intervention_type IN ('Pulvérisation', 'Semis')
            ORDER BY exit_time DESC
        """)
        interventions = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, type, amm, unit, culture, bbch, target, bio FROM catalog_products")
        catalog = {r['name']: dict(r) for r in cur.fetchall()}

        cur.execute("SELECT nom, code_oepp FROM cultures")
        cultures_oepp = {r['nom'].strip().lower(): r['code_oepp'] for r in cur.fetchall()}

        cur.execute("SELECT geofence_id, identifiant, surface_ha FROM parcelles")
        parcelles_rows = cur.fetchall()
        parcelles = {str(r['geofence_id']): r['identifiant'] for r in parcelles_rows}
        parcelles_surface = {int(r['geofence_id']): r['surface_ha'] for r in parcelles_rows if r['surface_ha']}

        cur.execute("SELECT siret, raison_sociale, applicateur, certiphyto FROM exploitation WHERE id = 1")
        row = cur.fetchone()
        siret = row['siret'] if row else ''
        raison_sociale = row['raison_sociale'] if row else ''
        applicateur = row['applicateur'] if row else ''
        certiphyto = row['certiphyto'] if row else ''

    raw = build_data()
    geofences = raw.get("geofences", {})

    wb = Workbook()
    ws = wb.active
    ws.title = "Registre Phyto"

    # En-tête exploitation
    ws.append(["SIRET", siret, "Raison sociale", raison_sociale])
    ws.append(["Applicateur", applicateur, "N° Certiphyto", certiphyto])
    ws.append([])
    ws.append(["Date", "Type", "ID Parcelle", "Nom Parcelle", "Surface (ha)",
               "Produit", "N° AMM", "Dose", "Cible", "Code culture", "Stade BBCH", "Bio"])

    for interv in interventions:
        geo_id_str = str(interv['geofence_id'])
        geo_name = geofences.get(geo_id_str, {}).get('name', f"Parcelle {interv['geofence_id']}")
        id_parcelle = parcelles.get(geo_id_str, '') or geo_name

        try:
            date_only = datetime.strptime(interv['exit_time'][:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            date_only = interv['exit_time'][:10]

        surf = interv.get('applied_area', '')

        try:
            products = json.loads(interv['products']) if interv['products'] else []
        except Exception:
            products = []

        if products:
            lignes_ecrites = 0
            for prod in products:
                prod_name = prod.get('name', '')
                cat = catalog.get(prod_name, {})
                # Le registre phyto exclut les engrais
                if cat.get('type') not in TYPES_AUTORISES_REGISTRE:
                    continue
                prod_dose = prod.get('dosage', '')
                unit = cat.get('unit', '')
                dose_display = f"{prod_dose} {unit}/ha".strip() if prod_dose else ''
                ws.append([
                    date_only, interv['intervention_type'], id_parcelle, geo_name, surf,
                    prod_name, cat.get('amm', ''), dose_display,
                    cat.get('target', ''), (cultures_oepp.get((cat.get('culture') or '').strip().lower(), '') or cat.get('culture', '')), cat.get('bbch', ''),
                    'Oui' if cat.get('bio') else 'Non'
                ])
                lignes_ecrites += 1
            if lignes_ecrites == 0:
                continue  # intervention 100% engrais : on ne l'inclut pas du tout
        else:
            continue  # pas de produits du tout : rien à mettre dans le registre phyto

    # Largeur des colonnes
    for col, width in zip('ABCDEFGHIJKL', [12,14,12,20,12,20,12,14,16,12,10,8]):
        ws.column_dimensions[col].width = width

    file = io.BytesIO()
    wb.save(file)
    file.seek(0)
    return send_file(file, as_attachment=True, download_name="registre_phyto.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def get_cultures_rules():
    """Retourne {nom_culture: {debut_mmdd, fin_mmdd}}."""
    DB_PATH = 'database.db'
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT nom, debut_mmdd, fin_mmdd FROM cultures")
        return {r['nom'].strip().lower(): {'debut_mmdd': r['debut_mmdd'], 'fin_mmdd': r['fin_mmdd']} for r in cur.fetchall()}


def campagne_label_in_bounds(dt, debut_mmdd, fin_mmdd):
    """
    Calcule le label de campagne auquel appartient dt, selon les bornes MM-DD d'une culture.
    - Campagne calendaire simple (ex: mais jan->dec) : retourne "2025"
    - Campagne hivernale a cheval (ex: ble sept N -> aout N+1) : retourne "2025-2026"
    """
    dmm, ddd = map(int, debut_mmdd.split('-'))
    fmm, fdd = map(int, fin_mmdd.split('-'))

    debut_avant_fin_meme_annee = (dmm, ddd) <= (fmm, fdd)

    if debut_avant_fin_meme_annee:
        # Campagne calendaire simple : label = l'annee de dt
        return str(dt.year)
    else:
        # Campagne hivernale qui traverse le 1er janvier
        # label = "NNNN-NNNN+1" ou NNNN est l'annee de debut de campagne
        seuil = datetime(dt.year, dmm, ddd)
        annee_debut = dt.year if dt >= seuil else dt.year - 1
        return f"{annee_debut}-{annee_debut + 1}"


def find_culture_for_intervention(geofence_id, exit_time_str, all_interventions, cultures_rules, products_catalog=None):
    """
    Détermine la culture d'une intervention en cherchant le PROCHAIN semis sur la même parcelle,
    et en vérifiant que la date de l'intervention tombe dans les bornes de campagne de cette culture.
    La culture d'un semis est résolue via le CATALOGUE PRODUITS (par nom de produit),
    car le champ 'culture' n'est pas stocké directement dans le produit de l'intervention.
    Retourne (nom_culture, campagne_label) ou (None, None) si indéterminé.
    """
    if products_catalog is None:
        products_catalog = {}

    try:
        dt_interv = datetime.strptime(exit_time_str[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        try:
            dt_interv = datetime.strptime(exit_time_str[:10], "%Y-%m-%d")
        except Exception:
            return None, None

    # Chercher tous les semis sur cette parcelle, après cette intervention, triés par date croissante
    semis_candidats = []
    for other in all_interventions:
        if other.get('geofence_id') != geofence_id:
            continue
        if other.get('intervention_type') != 'Semis':
            continue
        try:
            dt_semis = datetime.strptime(other['exit_time'][:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            try:
                dt_semis = datetime.strptime(other['exit_time'][:10], "%Y-%m-%d")
            except Exception:
                continue
        if dt_semis >= dt_interv:
            semis_candidats.append((dt_semis, other))
    semis_candidats.sort(key=lambda x: x[0])

    # Pour chaque semis candidat (du plus proche au plus lointain), résoudre la culture via le catalogue
    for dt_semis, semis_interv in semis_candidats:
        try:
            products = json.loads(semis_interv.get('products') or '[]')
        except Exception:
            products = []
        for prod in products:
            prod_name = (prod.get('name') or '').strip()
            if not prod_name:
                continue
            # Résoudre la culture via le catalogue (le produit lui-même ne stocke pas 'culture')
            cat_entry = products_catalog.get(prod_name, {})
            culture_nom = (cat_entry.get('culture') or '').strip()
            if not culture_nom:
                continue
            rule = cultures_rules.get(culture_nom.lower())
            if not rule:
                continue
            # Vérifier que dt_interv est dans les bornes de la campagne qui contient dt_semis
            label_semis = campagne_label_in_bounds(dt_semis, rule['debut_mmdd'], rule['fin_mmdd'])
            label_interv = campagne_label_in_bounds(dt_interv, rule['debut_mmdd'], rule['fin_mmdd'])
            if label_semis == label_interv:
                return culture_nom, label_semis
    return None, None


@app.route("/api/campagnes_disponibles")
@login_required
def campagnes_disponibles():
    """Liste les couples (culture, campagne) pour lesquels des interventions existent."""
    import traceback
    DB_PATH = 'database.db'
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT geofence_id, exit_time, intervention_type, products FROM interventions")
            all_interventions = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT name, culture FROM catalog_products")
            products_catalog = {r['name'].strip(): dict(r) for r in cur.fetchall()}

        cultures_rules = get_cultures_rules()
        combos = set()
        for interv in all_interventions:
            culture_nom, campagne_label = find_culture_for_intervention(
                interv['geofence_id'], interv['exit_time'], all_interventions, cultures_rules, products_catalog
            )
            if culture_nom and campagne_label:
                combos.add((culture_nom, campagne_label))

        # Trier par campagne décroissante (plus récente en premier) puis culture alphabétique
        result = sorted(combos, key=lambda x: (x[1], x[0]), reverse=True)
        return jsonify([{"culture": c, "campagne": y} for c, y in result])
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/bilan_annuel")
@login_required
def bilan_annuel():
    """Calcule le bilan par campagne de culture : quantité totale, surfaces, nb interventions, IFT."""
    DB_PATH = 'database.db'
    culture_filter = request.args.get("culture", "")
    campagne_filter = request.args.get("year", "")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT geofence_id, exit_time, intervention_type, products, applied_area
            FROM interventions
        """)
        all_interventions = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, type, amm, unit, dose, dar, bio, dose_homologuee, culture FROM catalog_products")
        catalog = {r['name'].strip(): dict(r) for r in cur.fetchall()}

    cultures_rules = get_cultures_rules()

    # Rattacher chaque intervention à sa culture/campagne, puis filtrer
    interventions = []
    for interv in all_interventions:
        culture_nom, campagne_label = find_culture_for_intervention(
            interv['geofence_id'], interv['exit_time'], all_interventions, cultures_rules, catalog
        )
        interv['_culture'] = culture_nom
        interv['_campagne'] = campagne_label
        if culture_filter and (culture_nom or '').lower() != culture_filter.lower():
            continue
        if campagne_filter and str(campagne_label) != str(campagne_filter):
            continue
        interventions.append(interv)

    # Surface de chaque parcelle : priorité surface cadastrale DB, sinon nom Traccar
    raw = build_data()
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

            # Quantité réelle utilisée pour CETTE intervention = dose/ha * surface traitée (ha)
            surf_cultivee = surface_parcelle.get(geo_id, surf_traitee) or surf_traitee
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


@app.route("/api/analytique")
@login_required
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
        raw    = build_data()
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
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT geofence_id, nom_parcelle, identifiant FROM parcelles")
        parcelle_map = {str(r["geofence_id"]): r["nom_parcelle"] or "" for r in cur.fetchall()}

        cur.execute("""
            SELECT device_id, geofence_id, exit_time,
                   vehicle_name, tool_detected, intervention_type,
                   applied_area, duration_min, products
            FROM interventions ORDER BY exit_time ASC
        """)
        rows = cur.fetchall()

        cur.execute("SELECT name, type, dose, dose_homologuee, unit FROM catalog_products")
        catalog = {r["name"]: dict(r) for r in cur.fetchall()}

    # ── Listes déroulantes ──
    all_vehicles  = sorted({(r["vehicle_name"] or "").strip() for r in rows if r["vehicle_name"]})
    all_tools     = sorted({(r["tool_detected"] or "").strip() for r in rows if r["tool_detected"]})
    all_types     = sorted({(r["intervention_type"] or "").strip() for r in rows if r["intervention_type"]})
    all_parcelles = sorted({parcelle_map.get(str(r["geofence_id"]), "") or f"Parcelle {r['geofence_id']}" for r in rows})

    # ── Helpers ──
    def parse_area(v):
        try:
            return max(0.0, float(str(v).replace(",", ".").replace(" ha", "").strip()))
        except:
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
        except:
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
        p_label = parcelle_map.get(geo_id) or f"Parcelle {geo_id}"

        if fv and fv not in vehicle.lower(): continue
        if fg and fg not in p_label.lower(): continue
        if ft and ft not in tool.lower():    continue
        if fi and fi not in itype.lower():   continue

        mois_key = ""
        try:
            mois_key = datetime.strptime(exit_time[:10], "%Y-%m-%d").strftime("%Y-%m")
        except:
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
        except:
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
            except:
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



# =========================================================================
# ROUTES FERTILISATION
# =========================================================================

@app.route("/api/objectifs_fertilisation", methods=["GET", "POST"])
@login_required
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


@app.route("/api/fertilisation")
@login_required
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
                       i.intervention_type, i.applied_area, i.products,
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

        # Utiliser les fonctions existantes du dashboard
        cultures_rules = get_cultures_rules()
        all_interv_for_culture = interventions  # toutes les interventions pour find_culture

        data_agg = {}

        for iv in interventions:
            prods = []
            try:
                prods = json.loads(iv["products"] or "[]")
            except:
                pass

            engrais = [p for p in prods if p.get("type") == "engrais" and p.get("name")]
            if not engrais:
                continue

            exit_time = iv["exit_time"] or ""
            area      = float(iv["applied_area"] or 0)
            geo_id    = iv["geofence_id"]
            nom_parc  = iv["nom_parcelle"] or f"Parcelle {geo_id}"

            # ── Utiliser la MÊME logique que le bilan IFT ──
            culture_nom, campagne_label = find_culture_for_intervention(
                geo_id, exit_time, all_interv_for_culture, cultures_rules, products_catalog
            )
            culture   = culture_nom or "—"
            campagne  = campagne_label or ""

            # Si pas de culture trouvée, fallback sur l'année
            if not campagne:
                try:
                    campagne = str(datetime.strptime(exit_time[:10], "%Y-%m-%d").year)
                except:
                    campagne = ""

            # Filtres
            if campagne_f and str(campagne) != str(campagne_f): continue
            if culture_f  and culture != culture_f:             continue
            if geo_f      and str(geo_id) != str(geo_f):        continue

            key = (geo_id, campagne, culture, nom_parc)
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

        for key, v in sorted(data_agg.items(), key=lambda x: (x[0][1], x[0][3])):
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

        all_campagnes = sorted({r["campagne"] for r in result}, reverse=True)
        all_cultures  = sorted({r["culture"]  for r in result if r["culture"] != "—"})

        return jsonify({
            "parcelles":     result,
            "totaux":        {k: round(v, 1) for k, v in totaux.items()},
            "objectifs":     objectifs,
            "all_campagnes": all_campagnes,
            "all_cultures":  all_cultures,
        })

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500



# =========================================================================
# ROUTE CONFIG TRACCAR
# =========================================================================

@app.route("/api/config_traccar", methods=["GET", "POST"])
@login_required
def api_config_traccar():
    global TRACCAR_URL, TRACCAR_USER, TRACCAR_PASSWORD, DAYS_BACK, CACHE_DURATION, _cached_data, _last_cache_time
    import json as _json
    config_path = "config.json"
    if request.method == "GET":
        cfg = {}
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f: cfg = _json.load(f)
        except: pass
        tcfg = cfg.get("traccar", {})
        return jsonify({"url": tcfg.get("url", TRACCAR_URL), "user": tcfg.get("user", TRACCAR_USER),
                        "password": "", "days_back": tcfg.get("days_back", DAYS_BACK),
                        "cache_duration": tcfg.get("cache_duration", CACHE_DURATION)})
    else:
        data = request.get_json()
        cfg = {}
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f: cfg = _json.load(f)
        except: pass
        cfg["traccar"] = {"url": data.get("url","").strip(), "user": data.get("user","").strip(),
                          "password": data.get("password","").strip(),
                          "days_back": int(data.get("days_back", 30)),
                          "cache_duration": int(data.get("cache_duration", 60))}
        with open(config_path, "w") as f: _json.dump(cfg, f, indent=2)
        TRACCAR_URL = cfg["traccar"]["url"]; TRACCAR_USER = cfg["traccar"]["user"]
        if cfg["traccar"]["password"]: TRACCAR_PASSWORD = cfg["traccar"]["password"]
        DAYS_BACK = cfg["traccar"]["days_back"]; CACHE_DURATION = cfg["traccar"]["cache_duration"]
        session.auth = (TRACCAR_USER, TRACCAR_PASSWORD)
        _cached_data = None; _last_cache_time = 0
        return jsonify({"status": "ok", "message": "Configuration Traccar mise à jour et appliquée."})


# =========================================================================
# EXPORT PDF — CAHIER DE FERTILISATION
# =========================================================================

@app.route("/export_pdf_fertilisation")
@login_required
def export_pdf_fertilisation():
    import traceback
    try:
        DB_PATH = 'database.db'
        campagne_f = request.args.get('campagne', '')
        culture_f  = request.args.get('culture', '')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row; cur = conn.cursor()
            cur.execute("SELECT i.geofence_id,i.exit_time,i.applied_area,i.products,p.nom_parcelle FROM interventions i LEFT JOIN parcelles p ON i.geofence_id=p.geofence_id ORDER BY i.exit_time ASC")
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
        cultures_rules=get_cultures_rules()
        agg={}
        for iv in interventions:
            prods=[]
            try: prods=json.loads(iv['products'] or '[]')
            except: pass
            engrais=[p for p in prods if p.get('type')=='engrais' and p.get('name')]
            if not engrais: continue
            geo_id=iv['geofence_id']; nom_parc=iv['nom_parcelle'] or f"Parcelle {geo_id}"
            area=float(iv['applied_area'] or 0); exit_t=iv['exit_time'] or ''
            culture_nom,campagne_label=find_culture_for_intervention(geo_id,exit_t,interventions,cultures_rules,products_catalog)
            culture=culture_nom or '—'; campagne=campagne_label or ''
            if campagne_f and str(campagne)!=str(campagne_f): continue
            if culture_f  and culture!=culture_f: continue
            key=(nom_parc,campagne,culture)
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
        for (nom_parc,campagne,culture),v in sorted(agg.items()):
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


# =========================================================================
# EXPORT PDF — ANALYTIQUE
# =========================================================================

@app.route("/export_pdf_analytique")
@login_required
def export_pdf_analytique():
    try:
        DB_PATH='database.db'; start_str=request.args.get('start',''); end_str=request.args.get('end','')
        f_vehicle=request.args.get('vehicle',''); f_geo=request.args.get('geofence','')
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory=sqlite3.Row; cur=conn.cursor()
            cur.execute("SELECT geofence_id,nom_parcelle FROM parcelles")
            parcelle_map={str(r['geofence_id']):r['nom_parcelle'] or '' for r in cur.fetchall()}
            cur.execute("SELECT device_id,geofence_id,exit_time,vehicle_name,tool_detected,intervention_type,applied_area,duration_min FROM interventions ORDER BY exit_time ASC")
            rows=cur.fetchall()
            cur.execute("SELECT raison_sociale FROM exploitation WHERE id=1")
            row2=cur.fetchone(); exploitation=row2['raison_sociale'] if row2 else ''
        def pa(v):
            try: return max(0.0,float(str(v).replace(',','.').replace(' ha','').strip()))
            except: return 0.0
        def ir(d):
            if not d: return True
            try:
                dt=datetime.strptime(d[:16],'%Y-%m-%dT%H:%M')
                if start_str and dt<datetime.strptime(start_str[:16],'%Y-%m-%dT%H:%M'): return False
                if end_str   and dt>datetime.strptime(end_str[:16],  '%Y-%m-%dT%H:%M'): return False
            except: pass
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
            geo=str(row['geofence_id'] or ''); pl=parcelle_map.get(geo) or f"Parcelle {geo}"
            if f_vehicle and f_vehicle.lower() not in veh.lower(): continue
            if f_geo     and f_geo.lower()     not in pl.lower():  continue
            mk=''
            try: mk=datetime.strptime(et[:10],'%Y-%m-%d').strftime('%Y-%m')
            except: pass
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
            except: ml=k
            tr([ml,v['passages'],round(v['surface'],2),fd(v['minutes']),fh(v['surface'],v['minutes'])],widths_m,fill=i%2==0)
        pdf.add_page(); sh('Chronologie',37,99,235)
        cols_t=['Date','Parcelle','Tracteur','Outil','Type','Surface','Durée','ha/h']
        widths_t=[pw*0.09,pw*0.17,pw*0.13,pw*0.13,pw*0.13,pw*0.10,pw*0.12,pw*0.13]
        th(cols_t,widths_t)
        for i,t in enumerate(tl):
            ds=t['date']
            try: y,m2,d=ds.split('-'); ds=f"{d}/{m2}/{y}"
            except: pass
            tr([ds,t['parcelle'],t['tracteur'],t['outil'],t['type'],f"{t['surface']} ha",fd(t['minutes']),fh(t['surface'],t['minutes'])],widths_t,fill=i%2==0)
        pdf.set_y(-12); pdf.set_font('Arial','I',8)
        from datetime import datetime as dt3
        pdf.cell(0,5,safe(f"Imprimé le {dt3.now().strftime('%d/%m/%Y à %H:%M')}"),align='R')
        os.makedirs('exports',exist_ok=True); path=os.path.join('exports','analytique.pdf')
        pdf.output(path); return send_file(path,as_attachment=True,download_name='rapport_analytique.pdf')
    except Exception as e:
        return f"Erreur PDF analytique : {e}<br><pre>{__import__('traceback').format_exc()}</pre>",500


if __name__ == "__main__":
    init_db()
    backup_database()
    threading.Thread(target=backup_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", debug=False, port=8080)
