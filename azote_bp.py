"""
Blueprint de calcul du previsionnel d'azote mineral, methode du bilan, conforme aux
annexes de l'arrete referentiel regional GREN Nouvelle-Aquitaine du 29 juillet 2025
(departements 19, 23, 24, 33, 40, 47, 64, 87 -- dont le 64), en vigueur depuis la
campagne 2025/2026.

Ce module a vocation a couvrir plusieurs cultures a terme (mais grain en V1, puis
ble/orge/avoine/triticale via l'Annexe 2.1.b, tournesol et colza via l'Annexe 2.4...).
Chaque culture est isolee dans ses propres tables de reference et sa propre fonction de
calcul (prefixees par culture, ex: calculer_dose_prev_mais), routees sous /api/azote/<culture>/...
pour ne jamais les faire interferer entre elles. Seul /api/parcelles/<id>/type_sol est
partage (le type de sol d'une parcelle ne depend pas de la culture qui y est implantee).

Perimetre actuel (V1, mais grain uniquement) -- volontairement restreint, extensible :
  - Mais grain uniquement (pas mais fourrage/semence/doux/sorgho pour l'instant)
  - Precedent "Cas n 1" de l'Annexe 2.2.b : precedent autre que prairie/legumineuse, sans
    culture intermediaire (couvre notamment mais sur mais, le cas declare par l'utilisateur)
  - Pas d'irrigation (Nirr = 0 par defaut, modifiable)
  - Reliquat sortie hiver NON mesure -> estimation via le precedent (methode b de l'annexe)
  - Type de sol : saisi manuellement par parcelle (les 8 categories exactes de l'annexe).
    Une carte pedologique nationale existe (GisSol/INRAE, WFS/WMS) mais a une resolution de
    1/250 000e -- trop grossiere pour fiabiliser une parcelle individuelle, et sans
    correspondance directe avec les 8 categories du GREN. Le declaratif manuel reste la
    source de verite pour un outil qui se veut proche du document officiel.

IMPORTANT -- avertissement a conserver visible cote utilisateur (deja discute) :
  Cet outil applique fidelement la methode et les valeurs de reference officielles, mais
  n'est PAS un outil labellise COMIFER au sens de l'arrete. Il peut servir de justificatif
  detaille (toutes les valeurs utilisees et leur source sont tracees), mais l'exploitant
  reste responsable de verifier ce point avec sa Chambre d'agriculture en cas de controle.

Tables de reference retranscrites depuis le PDF officiel (mais/sorgho, Annexe 2.2.b) :
https://www.nouvelle-aquitaine.developpement-durable.gouv.fr/IMG/pdf/2_2_b_mais_sorgho_bilan_cau_gren_na_vdef.pdf
"""
import os
import json
import sqlite3
import requests
from datetime import datetime, timedelta

from fpdf import FPDF
from flask import Blueprint, request, jsonify, send_file, redirect, url_for, session as flask_session

import dashboard

azote_bp = Blueprint("azote", __name__)

DB_PATH = "database.db"


@azote_bp.before_request
def _require_login():
    """
    Meme authentification que le reste de l'application -- avec la meme distinction que
    les autres blueprints : /export_pdf_azote n'est pas sous /api/ (telechargement direct),
    donc une session expiree doit rediriger vers la connexion plutot que renvoyer du JSON.
    """
    if not flask_session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Non authentifie", "redirect": "/login"}), 401
        return redirect(url_for("login"))


# =========================================================================
# TABLES DE REFERENCE OFFICIELLES (Annexe 2.2.b, GREN Nouvelle-Aquitaine 2025)
# =========================================================================

# Les 8 categories de sol utilisees par l'annexe (Rf, Mh, Ri apres CI, Ri tableau 8).
# Pour les categories marquees "utiliser la valeur de X" dans le texte officiel, la valeur
# de report est directement reprise ici (pas de cas particulier a gerer cote appelant).
SOIL_TYPES = [
    "argilo_calcaire",
    "sableux",
    "fond_vallees",       # "sols de fond de vallees, terres noires"
    "marais_argileux",    # renvoie vers fond_vallees (cf. note officielle)
    "limoneux",
    "terrasses_vallees",
    "granite",            # renvoie vers argileux_sablo (cf. note officielle)
    "argileux_sablo",
]

SOIL_LABELS = {
    "argilo_calcaire": "Sols argilo-calcaires",
    "sableux": "Sols sableux",
    "fond_vallees": "Sols de fond de vallees, terres noires",
    "marais_argileux": "Sols de marais argileux sodiques",
    "limoneux": "Sols limoneux",
    "terrasses_vallees": "Sols de terrasses de vallees",
    "granite": "Sols sur granite",
    "argileux_sablo": "Sols argileux a sablo-argileux",
}

# Annexe 7 (Types de sols) : aide a la reconnaissance terrain de chaque categorie -- noms
# vernaculaires locaux, texture, pH, cailloux typiques, classes de reserve utile (RU). Sert
# uniquement d'aide au choix cote utilisateur (pas utilise dans les calculs) ; a defaut de
# reference precise, le declaratif du GREN reste indicatif -- en cas de doute reel, une
# analyse de sol ou l'avis de la Chambre d'agriculture prime sur cette description.
SOIL_DESCRIPTIONS = {
    "argilo_calcaire": {
        "noms_vernaculaires": "Groies (superficielle, moyenne, profonde), aubues, champagnes, tuffeau",
        "texture_ph": "Argileux ou argilo-limono-sableux, pH > 7",
        "cailloux": "Calcaire, craie",
        "ru": "Faible <80mm / Moyenne 80-120mm / Elevee >120mm",
    },
    "sableux": {
        "noms_vernaculaires": "Sols sableux, doucins sableux hydromorphes, podzols (sables noirs/blancs)",
        "texture_ph": "Sableux a sablo-limoneux, pH < 7 (podzols : pH < 5)",
        "cailloux": "Gravier",
        "ru": "Faible <80mm / Moyenne 80-120mm",
    },
    "fond_vallees": {
        "noms_vernaculaires": "Marais tourbeux ou fond de vallee, touyas, terres noires de vallees",
        "texture_ph": "Argileux, acide ou neutre",
        "cailloux": "-",
        "ru": "Moyenne 80-120mm / Elevee >120mm",
    },
    "marais_argileux": {
        "noms_vernaculaires": "Marais argileux",
        "texture_ph": "Argileux, pH > 7",
        "cailloux": "-",
        "ru": "Moyenne 80-120mm / Elevee >120mm",
    },
    "limoneux": {
        "noms_vernaculaires": "Bornais, limons, sols limono-argileux a argilo-limoneux, doucins limoneux, limons sur schistes/gneiss",
        "texture_ph": "Limoneux a limono-argileux, pH < 7",
        "cailloux": "Calcaire et graviers, ou schistes selon le secteur",
        "ru": "Faible <80mm / Moyenne 80-120mm / Elevee >120mm",
    },
    "terrasses_vallees": {
        "noms_vernaculaires": "Alluvions, colluvions (hydromorphes ou non), boulbenes, alluvions sableuses et caillouteuses",
        "texture_ph": "Sablo-limoneux a limono-sableux, pH < 7 (parfois neutre)",
        "cailloux": "Gravier de quartz",
        "ru": "Faible <80mm / Moyenne 80-120mm / Elevee >120mm",
    },
    "granite": {
        "noms_vernaculaires": "Sable sur granite, sables limoneux",
        "texture_ph": "Limono-sableux, pH < 7",
        "cailloux": "Granite et quartz",
        "ru": "Faible <80mm / Moyenne 80-120mm / Elevee >120mm",
    },
    "argileux_sablo": {
        "noms_vernaculaires": "Argile a silex, brandes, doucins argileux, terreforts, palus, "
                               "coteaux molassiques du piemont pyreneen, silex, galets",
        "texture_ph": "Limono-argileux a argileux, pH < 7",
        "cailloux": "Silex",
        "ru": "Faible <80mm / Moyenne 80-120mm / Elevee >120mm",
    },
}

# Tableau 1 : besoin unitaire b (kgN/quintal) pour le mais grain, selon le niveau de
# rendement vise -- utilise directement les bornes officielles.
def _besoin_unitaire_mais_grain(objectif_rendement_q_ha):
    if objectif_rendement_q_ha is None:
        return None
    if objectif_rendement_q_ha < 100:
        return 2.3
    elif objectif_rendement_q_ha <= 120:
        return 2.2
    else:
        return 2.1


# Tableau 4 : Rf, azote non extractible par la culture (kgN/ha)
RF_PAR_SOL = {
    "argilo_calcaire": 30,
    "sableux": 10,
    "fond_vallees": 30,
    "marais_argileux": 30,   # = valeur "fond_vallees" (report officiel)
    "limoneux": 30,
    "terrasses_vallees": 15,
    "granite": 40,           # = valeur "argileux_sablo" (report officiel)
    "argileux_sablo": 40,
}

# Tableau 6 : valeur A (mineralisation climatique de l'annee precedente), a defaut de la
# valeur annuelle Arvalis publiee en debut de campagne sur le site de la DREAL.
VALEUR_A = {
    "fort": 160,     # climat chaud et humide
    "moyen": 120,    # annee normale
    "faible": 70,    # climat froid et sec
}

# Tableau 7 (extrait pertinent pour le mais/cereales -- precedents les plus courants avant
# mais en Nouvelle-Aquitaine). bp = besoin N unitaire du precedent (kgN/q ou kgN/t) ;
# coef_rpr = coefficient de correction d'un exces d'azote du bilan de la culture precedente.
PRECEDENTS_CAS1 = {
    "mais_grain":        {"label": "Mais grain",               "bp": 2.2,  "coef_rpr": 0.48},
    "mais_fourrage":     {"label": "Mais fourrage",             "bp": 13.0, "coef_rpr": 0.48},
    "mais_semence":      {"label": "Mais semence",              "bp": 5.7,  "coef_rpr": 0.48},
    "mais_doux_epis_spathes":    {"label": "Mais doux (epis + spathes)",   "bp": 12.0, "coef_rpr": 0.48},
    "mais_doux_epis_depouilles": {"label": "Mais doux (epis depouilles)",  "bp": 10.0, "coef_rpr": 0.48},
    "sorgho_grain":      {"label": "Sorgho grain",              "bp": 2.8,  "coef_rpr": 0.48},
    "sorgho_ensilage":   {"label": "Sorgho ensilage",           "bp": 13.0, "coef_rpr": 0.48},
    "ble_tendre_enleve": {"label": "Ble tendre (pailles enlevees)", "bp": 3.0,  "coef_rpr": 0.27},
    "ble_tendre_restit": {"label": "Ble tendre (pailles restituees)", "bp": 3.3, "coef_rpr": 0.27},
    "orge_enleve":       {"label": "Orge (pailles enlevees)",   "bp": 2.5,  "coef_rpr": 0.27},
    "orge_restit":       {"label": "Orge (pailles restituees)", "bp": 2.8,  "coef_rpr": 0.27},
    "tournesol":         {"label": "Tournesol",                 "bp": 4.0,  "coef_rpr": 0.40},
    "colza":             {"label": "Colza",                     "bp": 7.0,  "coef_rpr": 0.40},
}

# Tableau 8 : Ri (kgN/ha) par type de sol, selon l'APL (azote potentiellement lixiviable,
# kgN/ha) et la pluviometrie cumulee du 01/10 au 01/05 (mm). Recopie integrale du tableau
# officiel. Cles = (type_sol) -> liste de (APL, {pluie_mm: Ri}).
TABLEAU_8_RI = {
    "argilo_calcaire": {
        0:   {250: 36, 300: 35, 350: 32, 400: 29, 450: 26, 500: 24, 600: 21, 700: 20, 800: 20},
        20:  {250: 51, 300: 48, 350: 44, 400: 38, 450: 32, 500: 27, 600: 22, 700: 21, 800: 20},
        40:  {250: 67, 300: 62, 350: 56, 400: 46, 450: 37, 500: 30, 600: 23, 700: 21, 800: 20},
        60:  {250: 82, 300: 76, 350: 67, 400: 55, 450: 43, 500: 33, 600: 24, 700: 21, 800: 20},
        80:  {250: 98, 300: 90, 350: 79, 400: 64, 450: 49, 500: 37, 600: 25, 700: 21, 800: 20},
        100: {250: 113, 300: 104, 350: 91, 400: 73, 450: 54, 500: 40, 600: 25, 700: 21, 800: 20},
    },
    "sableux": {
        0:   {250: 71, 300: 58, 350: 44, 400: 35, 450: 32, 500: 31, 600: 30, 700: 30, 800: 30},
        20:  {250: 86, 300: 69, 350: 49, 400: 37, 450: 33, 500: 31, 600: 30, 700: 30, 800: 30},
        40:  {250: 102, 300: 79, 350: 55, 400: 40, 450: 33, 500: 31, 600: 30, 700: 30, 800: 30},
        60:  {250: 117, 300: 90, 350: 60, 400: 42, 450: 34, 500: 31, 600: 30, 700: 30, 800: 30},
        80:  {250: 132, 300: 100, 350: 65, 400: 44, 450: 35, 500: 32, 600: 30, 700: 30, 800: 30},
        100: {250: 148, 300: 111, 350: 70, 400: 46, 450: 35, 500: 32, 600: 30, 700: 30, 800: 30},
    },
    "fond_vallees": {
        0:   {250: 61, 300: 60, 350: 57, 400: 53, 450: 47, 500: 41, 600: 35, 700: 34, 800: 33},
        20:  {250: 76, 300: 74, 350: 69, 400: 62, 450: 54, 500: 46, 600: 37, 700: 34, 800: 33},
        40:  {250: 90, 300: 87, 350: 82, 400: 72, 450: 61, 500: 50, 600: 38, 700: 34, 800: 33},
        60:  {250: 105, 300: 101, 350: 94, 400: 82, 450: 68, 500: 54, 600: 39, 700: 34, 800: 33},
        80:  {250: 119, 300: 115, 350: 106, 400: 92, 450: 75, 500: 58, 600: 40, 700: 35, 800: 34},
        100: {250: 134, 300: 128, 350: 118, 400: 102, 450: 82, 500: 63, 600: 41, 700: 35, 800: 34},
    },
    "limoneux": {
        0:   {250: 50, 300: 48, 350: 46, 400: 41, 450: 36, 500: 32, 600: 27, 700: 26, 800: 26},
        20:  {250: 66, 300: 63, 350: 59, 400: 52, 450: 44, 500: 37, 600: 29, 700: 27, 800: 26},
        40:  {250: 81, 300: 78, 350: 72, 400: 62, 450: 51, 500: 41, 600: 30, 700: 28, 800: 27},
        60:  {250: 97, 300: 93, 350: 85, 400: 72, 450: 58, 500: 46, 600: 32, 700: 28, 800: 27},
        80:  {250: 113, 300: 107, 350: 98, 400: 83, 450: 66, 500: 50, 600: 34, 700: 29, 800: 28},
        100: {250: 129, 300: 122, 350: 111, 400: 93, 450: 73, 500: 55, 600: 35, 700: 30, 800: 29},
    },
    "terrasses_vallees": {
        0:   {250: 50, 300: 47, 350: 44, 400: 39, 450: 35, 500: 31, 600: 28, 700: 27, 800: 26},
        20:  {250: 65, 300: 61, 350: 55, 400: 48, 450: 40, 500: 34, 600: 28, 700: 27, 800: 26},
        40:  {250: 80, 300: 75, 350: 67, 400: 56, 450: 45, 500: 37, 600: 29, 700: 27, 800: 27},
        60:  {250: 95, 300: 88, 350: 78, 400: 64, 450: 50, 500: 40, 600: 30, 700: 27, 800: 27},
        80:  {250: 110, 300: 102, 350: 89, 400: 73, 450: 56, 500: 43, 600: 31, 700: 27, 800: 27},
        100: {250: 125, 300: 116, 350: 101, 400: 81, 450: 61, 500: 46, 600: 31, 700: 28, 800: 27},
    },
    "argileux_sablo": {
        0:   {250: 24, 300: 23, 350: 22, 400: 20, 450: 19, 500: 17, 600: 16, 700: 16, 800: 16},
        20:  {250: 39, 300: 37, 350: 33, 400: 28, 450: 24, 500: 20, 600: 17, 700: 16, 800: 16},
        40:  {250: 54, 300: 50, 350: 44, 400: 37, 450: 29, 500: 23, 600: 18, 700: 16, 800: 16},
        60:  {250: 69, 300: 64, 350: 56, 400: 45, 450: 34, 500: 26, 600: 18, 700: 16, 800: 16},
        80:  {250: 84, 300: 77, 350: 67, 400: 53, 450: 39, 500: 29, 600: 19, 700: 16, 800: 16},
        100: {250: 99, 300: 91, 350: 78, 400: 61, 450: 44, 500: 31, 600: 20, 700: 17, 800: 16},
    },
}
# "Sols de marais argileux sodiques" et "Sols sur granite" : le PDF officiel ne donne pas de
# valeurs propres (case "-") -- reports respectifs vers fond_vallees et argileux_sablo,
# cohérent avec les reports deja faits pour Rf/Mh.
TABLEAU_8_RI["marais_argileux"] = TABLEAU_8_RI["fond_vallees"]
TABLEAU_8_RI["granite"] = TABLEAU_8_RI["argileux_sablo"]

# Tableau 12 : Mh, mineralisation nette de l'humus (kgN/ha) -- mais/sorgho grain+ensilage SEC
# (colonne IRRIGUE non utilisee ici puisque l'exploitation est en sec).
MH_PAR_SOL_SEC = {
    "argilo_calcaire": 25,
    "sableux": 55,
    "fond_vallees": 35,
    "marais_argileux": 35,
    "limoneux": 45,
    "terrasses_vallees": 45,
    "granite": 30,
    "argileux_sablo": 30,
}

# Tableau 14 (extrait) : Mr, mineralisation nette des residus de recolte du precedent
# (kgN/ha). Pour le mais (culture d'ete), le bilan s'ouvre en avril -> colonne "avril".
MR_PAR_PRECEDENT_AVRIL = {
    "mais_grain": 0,
    "mais_fourrage": 0,
    "mais_semence": 0,
    "mais_doux_epis_spathes": 0,
    "mais_doux_epis_depouilles": 0,
    "sorgho_grain": 0,
    "sorgho_ensilage": 0,
    "ble_tendre_enleve": 0,    # Tableau 14 : "Cereales, pailles enlevees ou brulees" -> avril = 0
    "ble_tendre_restit": -10,  # Tableau 14 : "Cereales, pailles enfouies" -> avril = -10
    "orge_enleve": 0,          # idem ble_tendre_enleve : pailles enlevees -> avril = 0
    "orge_restit": -10,        # idem ble_tendre_restit : pailles enfouies -> avril = -10
    "tournesol": 0,
    "colza": 10,
}

# CAU (coefficient apparent d'utilisation) selon le stade d'apport, mais/sorgho grain et fourrage.
CAU_AVANT_4_FEUILLES = 0.6
CAU_APRES_4_FEUILLES = 0.8
PLAFOND_APPORT_AVANT_4_FEUILLES = 50  # kgN/ha, plafond reglementaire (semis avant le 1er mai)


# =========================================================================
# TABLES SPECIFIQUES CEREALES A PAILLE (Annexe 2.1.b, meme groupe de departements)
# =========================================================================

# Tableau 1 (Annexe 2.1.b) : besoin unitaire b (kgN/quintal) par cereale.
B_CEREALES = {
    "triticale": 2.6,
    "seigle": 2.3,
    "orge": 2.5,
    "avoine": 2.2,
    "autres_cereales": 2.5,
    "ble_tendre_hiver": 3.0,
    "ble_dur": 3.7,
    "ble_tendre_ameliorant": 3.5,
}

CEREALE_LABELS = {
    "triticale": "Triticale",
    "seigle": "Seigle",
    "orge": "Orge",
    "avoine": "Avoine",
    "autres_cereales": "Autres cereales / melanges",
    "ble_tendre_hiver": "Ble tendre d'hiver",
    "ble_dur": "Ble dur",
    "ble_tendre_ameliorant": "Ble tendre ameliorant",
}

# NOTE : le Tableau 4 (Pi selon nombre de talles) sert uniquement a la METHODE A -- mesure
# directe du reliquat sortie hiver (Ri mesure au laboratoire) + Pi estime separement selon
# le stade de la culture. Cette V1 n'implemente que la METHODE B (estimation via precedent +
# pluviometrie), qui donne directement (Ri+Pi) COMBINES par le Tableau 10 -- le nombre de
# talles n'intervient donc pas dans ce calcul. Un champ "nombre_talles" a ete retire du
# formulaire suite a une revue de coherence : il etait collecte sans jamais influencer le
# resultat, ce qui aurait pu laisser croire a tort qu'il comptait dans le calcul.

# Tableau 11 (Annexe 2.1.b) : Mh, mineralisation nette de l'humus (kgN/ha), cereales a
# paille -- une seule colonne (pas de distinction sec/irrigue comme pour le mais).
MH_CEREALES = {
    "argilo_calcaire": 20,
    "sableux": 40,
    "fond_vallees": 30,
    "marais_argileux": 30,
    "limoneux": 35,
    "terrasses_vallees": 35,
    "granite": 25,
    "argileux_sablo": 25,
}

# Tableau 13 (Annexe 2.1.b) : identique au tableau 14 du mais (meme table nationale
# COMIFER), mais pour les cereales d'hiver le bilan s'ouvre en SORTIE D'HIVER (pas en
# avril comme pour le mais) -- colonne differente du meme tableau source.
MR_PAR_PRECEDENT_SORTIE_HIVER = {
    "mais_grain": -10,
    "mais_fourrage": 0,
    "mais_semence": -10,
    "mais_doux_epis_spathes": -10,
    "mais_doux_epis_depouilles": -10,
    "sorgho_grain": -10,
    "sorgho_ensilage": -10,
    "ble_tendre_enleve": 0,
    "ble_tendre_restit": -20,
    "orge_enleve": 0,
    "orge_restit": -20,
    "tournesol": -10,
    "colza": 20,
}

# Tableau 10 (Annexe 2.1.b) : Ri+Pi (kgN/ha) par type de sol, selon l'APL et la
# pluviometrie cumulee du 01/10 au 01/03 (bilan hiver -- different du 01/10-01/05 du mais).
TABLEAU_10_RI_PI = {
    "argilo_calcaire": {
        0:   {150: 30, 200: 30, 250: 29, 300: 29, 350: 28, 400: 27, 450: 27, 500: 27, 600: 26},
        20:  {150: 45, 200: 44, 250: 42, 300: 40, 350: 37, 400: 33, 450: 30, 500: 28, 600: 27},
        40:  {150: 59, 200: 58, 250: 55, 300: 51, 350: 45, 400: 39, 450: 33, 500: 30, 600: 27},
        60:  {150: 74, 200: 72, 250: 68, 300: 62, 350: 53, 400: 44, 450: 37, 500: 32, 600: 27},
        80:  {150: 89, 200: 86, 250: 82, 300: 73, 350: 62, 400: 50, 450: 40, 500: 33, 600: 28},
        100: {150: 104, 200: 101, 250: 95, 300: 84, 350: 70, 400: 55, 450: 43, 500: 35, 600: 28},
    },
    "sableux": {
        0:   {150: 53, 200: 45, 250: 31, 300: 25, 350: 24, 400: 24, 450: 24, 500: 24, 600: 24},
        20:  {150: 72, 200: 58, 250: 35, 300: 25, 350: 24, 400: 24, 450: 24, 500: 24, 600: 24},
        40:  {150: 90, 200: 71, 250: 39, 300: 26, 350: 24, 400: 24, 450: 24, 500: 24, 600: 24},
        60:  {150: 108, 200: 83, 250: 43, 300: 27, 350: 24, 400: 24, 450: 24, 500: 24, 600: 24},
        80:  {150: 126, 200: 96, 250: 47, 300: 28, 350: 24, 400: 24, 450: 24, 500: 24, 600: 24},
        100: {150: 144, 200: 109, 250: 52, 300: 28, 350: 24, 400: 24, 450: 24, 500: 24, 600: 24},
    },
    "fond_vallees": {
        0:   {150: 50, 200: 49, 250: 48, 300: 46, 350: 44, 400: 41, 450: 39, 500: 38, 600: 37},
        20:  {150: 64, 200: 63, 250: 61, 300: 57, 350: 51, 400: 45, 450: 41, 500: 39, 600: 38},
        40:  {150: 78, 200: 76, 250: 73, 300: 67, 350: 58, 400: 49, 450: 43, 500: 40, 600: 38},
        60:  {150: 92, 200: 89, 250: 85, 300: 77, 350: 65, 400: 54, 450: 45, 500: 41, 600: 38},
        80:  {150: 105, 200: 103, 250: 97, 300: 87, 350: 72, 400: 58, 450: 48, 500: 42, 600: 38},
        100: {150: 119, 200: 116, 250: 109, 300: 97, 350: 80, 400: 62, 450: 50, 500: 43, 600: 38},
    },
    "limoneux": {
        0:   {150: 40, 200: 39, 250: 38, 300: 36, 350: 34, 400: 33, 450: 32, 500: 32, 600: 32},
        20:  {150: 54, 200: 52, 250: 49, 300: 43, 350: 38, 400: 34, 450: 33, 500: 32, 600: 32},
        40:  {150: 68, 200: 65, 250: 59, 300: 50, 350: 42, 400: 36, 450: 33, 500: 32, 600: 32},
        60:  {150: 83, 200: 79, 250: 70, 300: 57, 350: 45, 400: 38, 450: 34, 500: 33, 600: 32},
        80:  {150: 97, 200: 92, 250: 81, 300: 65, 350: 49, 400: 39, 450: 35, 500: 33, 600: 32},
        100: {150: 111, 200: 105, 250: 91, 300: 72, 350: 53, 400: 41, 450: 35, 500: 33, 600: 32},
    },
    "terrasses_vallees": {
        0:   {150: 41, 200: 41, 250: 40, 300: 38, 350: 36, 400: 34, 450: 33, 500: 32, 600: 32},
        20:  {150: 56, 200: 55, 250: 52, 300: 47, 350: 41, 400: 36, 450: 34, 500: 33, 600: 32},
        40:  {150: 71, 200: 69, 250: 65, 300: 57, 350: 47, 400: 39, 450: 35, 500: 33, 600: 32},
        60:  {150: 86, 200: 84, 250: 78, 300: 66, 350: 52, 400: 42, 450: 36, 500: 34, 600: 32},
        80:  {150: 101, 200: 98, 250: 90, 300: 76, 350: 58, 400: 44, 450: 37, 500: 34, 600: 32},
        100: {150: 116, 200: 112, 250: 103, 300: 85, 350: 64, 400: 47, 450: 38, 500: 34, 600: 32},
    },
    "argileux_sablo": {
        0:   {150: 22, 200: 22, 250: 22, 300: 22, 350: 22, 400: 22, 450: 21, 500: 21, 600: 21},
        20:  {150: 37, 200: 36, 250: 34, 300: 32, 350: 28, 400: 25, 450: 23, 500: 22, 600: 22},
        40:  {150: 52, 200: 50, 250: 47, 300: 41, 350: 35, 400: 29, 450: 25, 500: 23, 600: 22},
        60:  {150: 67, 200: 64, 250: 59, 300: 51, 350: 41, 400: 33, 450: 27, 500: 24, 600: 22},
        80:  {150: 82, 200: 78, 250: 71, 300: 61, 350: 48, 400: 37, 450: 29, 500: 25, 600: 22},
        100: {150: 97, 200: 92, 250: 84, 300: 70, 350: 54, 400: 41, 450: 31, 500: 26, 600: 22},
    },
}
TABLEAU_10_RI_PI["marais_argileux"] = TABLEAU_10_RI_PI["fond_vallees"]
TABLEAU_10_RI_PI["granite"] = TABLEAU_10_RI_PI["argileux_sablo"]

CAU_CEREALES = 0.9  # 0.8 possible a titre exceptionnel et justifie (cf article 9, Annexe 2.1.b)


def calculer_dose_prev_ble(params):
    """
    Calcule la dose previsionnelle d'azote mineral pour une cereale a paille (ble, orge,
    avoine, triticale, seigle), methode du bilan CAU, Annexe 2.1.b, Cas n 1 (precedent
    hors prairie/legumineuse). Structure identique a calculer_dose_prev_mais, mais equation
    a un seul temps (pas de split avant/apres un stade -- CAU fixe a 0.9).

    params attendus (dict) :
      - cereale : str, une des cles de B_CEREALES
      - objectif_rendement, type_sol, precedent, rendement_precedent,
        azote_apporte_precedent, valeur_a_choix, pluie_cumulee_mm (01/10-01/03 ici),
        nirr, xa, mhp, mrci : memes significations que pour le mais
      - cau : float, 0.9 par defaut (0.8 possible, exceptionnel et justifie)
    """
    cereale = params["cereale"]
    type_sol = params["type_sol"]
    precedent = params["precedent"]
    if cereale not in B_CEREALES:
        raise ValueError(f"Cereale inconnue : {cereale}")
    if type_sol not in SOIL_TYPES:
        raise ValueError(f"Type de sol inconnu : {type_sol}")
    if precedent not in PRECEDENTS_CAS1:
        raise ValueError(f"Precedent non couvert par cette V1 (Cas n 1 uniquement) : {precedent}")

    objectif_rendement = float(params["objectif_rendement"])
    rendement_precedent = float(params.get("rendement_precedent") or 0)
    azote_apporte_precedent = float(params.get("azote_apporte_precedent") or 0)
    valeur_a_choix = params.get("valeur_a_choix", "moyen")
    valeur_a = VALEUR_A.get(valeur_a_choix, VALEUR_A["moyen"])
    pluie_cumulee_mm = float(params.get("pluie_cumulee_mm") or 350)
    nirr = float(params.get("nirr") or 0)
    xa = float(params.get("xa") or 0)
    mhp = float(params.get("mhp") or 0)
    mrci = float(params.get("mrci") or 0)
    cau = float(params.get("cau") or CAU_CEREALES)
    appliquer_plancher_30 = params.get("appliquer_plancher_30", True)

    # --- Pf : besoin de la culture a la fermeture du bilan ---
    b = B_CEREALES[cereale]
    pf = b * objectif_rendement

    # --- Rf : azote non extractible (tableau 3, memes valeurs que le mais -- tableau 4) ---
    rf = RF_PAR_SOL[type_sol]

    # --- Ri+Pi : reliquat + azote deja absorbe a l'ouverture du bilan ---
    prec_ref = PRECEDENTS_CAS1[precedent]
    pf_precedent = rendement_precedent * prec_ref["bp"]
    apl = (valeur_a + azote_apporte_precedent - pf_precedent) * prec_ref["coef_rpr"]
    apl = max(apl, 0)
    ri_plus_pi = _interpoler_table_ri(TABLEAU_10_RI_PI, type_sol, apl, pluie_cumulee_mm)

    # --- Mh : mineralisation nette de l'humus (tableau 11) ---
    mh = MH_CEREALES[type_sol]

    # --- Mr : mineralisation nette des residus du precedent (tableau 13, sortie hiver) ---
    mr = MR_PAR_PRECEDENT_SORTIE_HIVER.get(precedent, 0)

    fournitures = ri_plus_pi + mh + mhp + mr + mrci + nirr
    besoins = pf + rf
    # Xa a l'interieur du numerateur divise par CAU -- meme correctif que calculer_dose_prev_mais,
    # conforme a la mise en page exacte du texte officiel (Annexe 2.1.b) : la barre de fraction
    # couvre tout le numerateur, Xa inclus, avant division par CAU.
    numerateur = besoins - fournitures - xa
    dose = numerateur / cau

    dose_brute = dose
    if dose < 0:
        dose = 0
        regle_appliquee = "bilan negatif : aucun apport mineral"
    elif dose < 30 and appliquer_plancher_30:
        dose = 30
        regle_appliquee = "bilan entre 0 et 30 kgN/ha : ramene forfaitairement a 30 kgN/ha (faculte prevue par l'arrete)"
    else:
        regle_appliquee = None

    return {
        "resultat": {
            "dose_totale_kgN_ha": round(dose, 1),
            "dose_brute_kgN_ha": round(dose_brute, 1),
            "regle_plancher_appliquee": regle_appliquee,
        },
        "detail_bilan": {
            "Pf": {"valeur": round(pf, 1), "unite": "kgN/ha",
                   "formule": f"b({b}) x objectif_rendement({objectif_rendement} q/ha)",
                   "source": "Tableau 1, Annexe 2.1.b"},
            "Rf": {"valeur": rf, "unite": "kgN/ha", "formule": f"valeur fixe pour {SOIL_LABELS[type_sol]}",
                   "source": "Tableau 3, Annexe 2.1.b"},
            "Ri+Pi": {"valeur": ri_plus_pi, "unite": "kgN/ha",
                      "formule": f"APL={round(apl,1)} kgN/ha (precedent {prec_ref['label']}), "
                                 f"pluie cumulee 01/10-01/03: {pluie_cumulee_mm} mm, interpole",
                      "source": "Tableau 10 (Cas n 1), Annexe 2.1.b"},
            "Mh": {"valeur": mh, "unite": "kgN/ha", "formule": f"valeur fixe pour {SOIL_LABELS[type_sol]}",
                   "source": "Tableau 11, Annexe 2.1.b"},
            "Mhp": {"valeur": mhp, "unite": "kgN/ha", "formule": "saisie manuelle (0 par defaut)",
                    "source": "Tableau 12, Annexe 2.1.b"},
            "Mr": {"valeur": mr, "unite": "kgN/ha", "formule": f"precedent {prec_ref['label']}, ouverture du bilan en sortie hiver",
                   "source": "Tableau 13, Annexe 2.1.b"},
            "MrCi": {"valeur": mrci, "unite": "kgN/ha", "formule": "saisie manuelle (0 par defaut, cultures d'hiver: negligeable)",
                     "source": "Tableau 14, Annexe 2.1.b"},
            "Nirr": {"valeur": nirr, "unite": "kgN/ha", "formule": "saisie manuelle (0 : pas d'irrigation)",
                     "source": "Point 8, Annexe 2.1.b"},
            "Xa": {"valeur": xa, "unite": "kgN/ha", "formule": "saisie manuelle (0 par defaut)",
                   "source": "Point 10, Annexe 2.1.b"},
            "CAU": {"valeur": cau, "unite": "-", "source": "Point 9, Annexe 2.1.b (0,9 standard, 0,8 exceptionnel justifie)"},
        },
        "avertissement": (
            "Calcul realise selon la methode du bilan CAU de l'Annexe 2.1.b de l'arrete "
            "referentiel regional GREN Nouvelle-Aquitaine du 29 juillet 2025 (departements "
            "incluant le 64), a partir de valeurs de reference par defaut (reliquat non "
            "mesure). Cet outil n'est pas labellise COMIFER : verifier avec votre Chambre "
            "d'agriculture sa recevabilite en cas de controle."
        ),
    }


def _interpoler_table_ri(table_ri, type_sol, apl, pluie_mm):
    """
    Interpole lineairement une table Ri a deux axes (APL et pluviometrie cumulee), en
    bornant aux valeurs extremes si l'entree depasse la plage couverte. Generique :
    utilisee pour le tableau 8 (mais, pluie 01/10-01/05) et le tableau 10 (cereales a
    paille, pluie 01/10-01/03), qui ont la meme structure mais des plages/valeurs propres.
    Le reglement ne fournit que des points discrets -- l'interpolation lineaire est une
    approximation raisonnable entre ces points, dans le meme esprit que les outils de
    calcul assistes par ordinateur (aucune methode d'interpolation n'est imposee par le
    texte reglementaire).
    """
    table = table_ri[type_sol]
    apl_keys = sorted(table.keys())
    apl_c = min(max(apl, apl_keys[0]), apl_keys[-1])
    apl_lo = max([k for k in apl_keys if k <= apl_c])
    apl_hi = min([k for k in apl_keys if k >= apl_c])

    def _interp_pluie(row):
        pluie_keys = sorted(row.keys())
        p_c = min(max(pluie_mm, pluie_keys[0]), pluie_keys[-1])
        p_lo = max([k for k in pluie_keys if k <= p_c])
        p_hi = min([k for k in pluie_keys if k >= p_c])
        if p_lo == p_hi:
            return row[p_lo]
        t = (p_c - p_lo) / (p_hi - p_lo)
        return row[p_lo] + t * (row[p_hi] - row[p_lo])

    ri_lo = _interp_pluie(table[apl_lo])
    ri_hi = _interp_pluie(table[apl_hi])
    if apl_lo == apl_hi:
        return round(ri_lo, 1)
    t = (apl_c - apl_lo) / (apl_hi - apl_lo)
    return round(ri_lo + t * (ri_hi - ri_lo), 1)


def calculer_dose_prev_mais(params):
    """
    Calcule la dose previsionnelle d'azote mineral pour le mais grain, methode du bilan CAU,
    Annexe 2.2.b, Cas n 1 (precedent hors prairie/legumineuse, sans culture intermediaire).

    params attendus (dict) :
      - objectif_rendement : float, q/ha
      - type_sol : str, une des cles de SOIL_TYPES
      - precedent : str, une des cles de PRECEDENTS_CAS1
      - rendement_precedent : float, rendement reel du precedent (q/ha ou t/ha selon precedent)
      - azote_apporte_precedent : float, azote mineral + organique (equivalent) apporte au
        precedent, kgN/ha
      - valeur_a_choix : 'fort' | 'moyen' | 'faible'
      - pluie_cumulee_mm : float, cumul de pluie du 01/10 au 01/05 (mm)
      - nirr : float, azote apporte par irrigation, kgN/ha (0 si sec)
      - xa : float, equivalent engrais mineral des apports organiques recents, kgN/ha (0 par defaut)
      - mhp : float, mineralisation due a un retournement de prairie, kgN/ha (0 par defaut)
      - mrci : float, mineralisation des residus de culture intermediaire, kgN/ha (0 par defaut)
      - dose_avant_4_feuilles : float, dose d'azote mineral que l'exploitant prevoit d'apporter
        avant le stade 4 feuilles, kgN/ha (0 si apport unique après 4 feuilles)
      - appliquer_plancher_30 : bool, True par defaut. Le texte de l'arrete precise que la
        dose "PEUT" (faculte, pas obligation) etre ramenee forfaitairement a 30 kgN/ha si un
        bilan calcule est compris entre 0 et 30 -- "si la nature ou les modalites de l'apport
        ne permettent pas de s'assurer d'une pratique de fertilisation suffisamment precise."
        Mettre a False pour voir la valeur brute du calcul sans cet ajustement.

    Retourne un dict detaillant CHAQUE terme du bilan avec sa valeur et sa reference
    (tableau/annexe), pour tracabilite en cas de controle, plus le resultat final.
    """
    type_sol = params["type_sol"]
    precedent = params["precedent"]
    if type_sol not in SOIL_TYPES:
        raise ValueError(f"Type de sol inconnu : {type_sol}")
    if precedent not in PRECEDENTS_CAS1:
        raise ValueError(f"Precedent non couvert par cette V1 (Cas n 1 uniquement) : {precedent}")

    objectif_rendement = float(params["objectif_rendement"])
    rendement_precedent = float(params.get("rendement_precedent") or 0)
    azote_apporte_precedent = float(params.get("azote_apporte_precedent") or 0)
    valeur_a_choix = params.get("valeur_a_choix", "moyen")
    valeur_a = VALEUR_A.get(valeur_a_choix, VALEUR_A["moyen"])
    pluie_cumulee_mm = float(params.get("pluie_cumulee_mm") or 400)
    nirr = float(params.get("nirr") or 0)
    xa = float(params.get("xa") or 0)
    mhp = float(params.get("mhp") or 0)
    mrci = float(params.get("mrci") or 0)
    dose_avant_4f = min(float(params.get("dose_avant_4_feuilles") or 0), PLAFOND_APPORT_AVANT_4_FEUILLES)
    appliquer_plancher_30 = params.get("appliquer_plancher_30", True)

    # --- Pf : besoin de la culture a la fermeture du bilan ---
    b = _besoin_unitaire_mais_grain(objectif_rendement)
    pf = b * objectif_rendement

    # --- Rf : azote non extractible (tableau 4) ---
    rf = RF_PAR_SOL[type_sol]

    # --- Ri : reliquat a l'ouverture du bilan, estime via le precedent (methode b, cas 1) ---
    prec_ref = PRECEDENTS_CAS1[precedent]
    pf_precedent = rendement_precedent * prec_ref["bp"]
    apl = (valeur_a + azote_apporte_precedent - pf_precedent) * prec_ref["coef_rpr"]
    apl = max(apl, 0)  # l'APL ne peut pas etre negatif (pas de xa_avant_ouverture en V1)
    ri = _interpoler_table_ri(TABLEAU_8_RI, type_sol, apl, pluie_cumulee_mm)

    # --- Mh : mineralisation nette de l'humus (tableau 12, sec) ---
    mh = MH_PAR_SOL_SEC[type_sol]

    # --- Mr : mineralisation nette des residus du precedent (tableau 14, ouverture avril) ---
    mr = MR_PAR_PRECEDENT_AVRIL.get(precedent, 0)

    # --- Calcul en deux temps (avant / apres 4 feuilles) ---
    n_utile_avant_4f = dose_avant_4f * CAU_AVANT_4_FEUILLES

    fournitures = ri + mh + mhp + mr + mrci + nirr + n_utile_avant_4f
    besoins = pf + rf
    # Xa (equivalent engrais mineral des produits organiques) est A L'INTERIEUR du
    # numerateur divise par CAU -- confirme par la mise en page exacte du texte officiel :
    # "X = (Pf+Rf) - (Ri+Mh+Mhp+Mr+MrCi+Nirr+Nmineral avant 4 feuilles) - Xa" sur une seule
    # ligne, avec "CAU apres 4 feuilles" comme denominateur sur la ligne suivante (barre de
    # fraction couvrant tout le numerateur, Xa inclus). Ne pas soustraire Xa apres division.
    numerateur = besoins - fournitures - xa
    dose_apres_4f = numerateur / CAU_APRES_4_FEUILLES

    # Regle facultative de l'arrete (article precedant l'article 13) : bilan entre 0 et 30
    # -> peut etre ramene forfaitairement a 30 kgN/ha ; bilan negatif -> aucun apport (regle
    # de bon sens, non explicitement chiffree dans le texte mais logique -- on ne peut pas
    # epandre une dose negative).
    dose_apres_4f_brute = dose_apres_4f
    if dose_apres_4f < 0:
        dose_apres_4f = 0
        regle_appliquee = "bilan negatif : aucun apport mineral apres 4 feuilles"
    elif dose_apres_4f < 30 and appliquer_plancher_30:
        dose_apres_4f = 30
        regle_appliquee = "bilan entre 0 et 30 kgN/ha : ramene forfaitairement a 30 kgN/ha (faculte prevue par l'arrete)"
    else:
        regle_appliquee = None

    dose_totale = dose_avant_4f + dose_apres_4f

    return {
        "resultat": {
            "dose_avant_4_feuilles_kgN_ha": round(dose_avant_4f, 1),
            "dose_apres_4_feuilles_kgN_ha": round(dose_apres_4f, 1),
            "dose_apres_4_feuilles_brute_kgN_ha": round(dose_apres_4f_brute, 1),
            "dose_totale_kgN_ha": round(dose_totale, 1),
            "regle_plancher_appliquee": regle_appliquee,
        },
        "detail_bilan": {
            "Pf": {"valeur": round(pf, 1), "unite": "kgN/ha",
                   "formule": f"b({b}) x objectif_rendement({objectif_rendement} q/ha)",
                   "source": "Tableau 1, Annexe 2.2.b"},
            "Rf": {"valeur": rf, "unite": "kgN/ha", "formule": f"valeur fixe pour {SOIL_LABELS[type_sol]}",
                   "source": "Tableau 4, Annexe 2.2.b"},
            "Ri": {"valeur": ri, "unite": "kgN/ha",
                   "formule": f"APL={round(apl,1)} kgN/ha (precedent {prec_ref['label']}), "
                              f"pluie cumulee {pluie_cumulee_mm} mm, interpole",
                   "source": "Tableau 8 (Cas n 1), Annexe 2.2.b"},
            "Mh": {"valeur": mh, "unite": "kgN/ha", "formule": f"valeur fixe pour {SOIL_LABELS[type_sol]}, mais sec",
                   "source": "Tableau 12, Annexe 2.2.b"},
            "Mhp": {"valeur": mhp, "unite": "kgN/ha", "formule": "saisie manuelle (0 par defaut, pas de retournement de prairie)",
                    "source": "Tableau 13, Annexe 2.2.b"},
            "Mr": {"valeur": mr, "unite": "kgN/ha", "formule": f"precedent {prec_ref['label']}, ouverture du bilan en avril",
                   "source": "Tableau 14, Annexe 2.2.b"},
            "MrCi": {"valeur": mrci, "unite": "kgN/ha", "formule": "saisie manuelle (0 par defaut, pas de culture intermediaire)",
                     "source": "Tableau 15, Annexe 2.2.b"},
            "Nirr": {"valeur": nirr, "unite": "kgN/ha", "formule": "saisie manuelle (0 : pas d'irrigation)",
                     "source": "Point 8, Annexe 2.2.b"},
            "Xa": {"valeur": xa, "unite": "kgN/ha", "formule": "saisie manuelle (0 par defaut, pas d'apport organique recent)",
                   "source": "Point 10, Annexe 2.2.b"},
            "Dose avant 4 feuilles": {"valeur": round(dose_avant_4f, 1), "unite": "kgN/ha",
                   "formule": "saisie manuelle, plafonnee a 50 kgN/ha (Point 9, Annexe 2.2.b)",
                   "source": "Parametre utilisateur"},
            "N utile avant 4 feuilles": {"valeur": round(n_utile_avant_4f, 1), "unite": "kgN/ha",
                   "formule": f"dose avant 4 feuilles ({round(dose_avant_4f,1)}) x CAU avant ({CAU_AVANT_4_FEUILLES}) "
                              f"-- soustrait des fournitures pour le calcul de la dose apres 4 feuilles",
                   "source": "Calcul intermediaire"},
            "CAU_avant_4_feuilles": {"valeur": CAU_AVANT_4_FEUILLES, "unite": "-", "source": "Tableau 17, Annexe 2.2.b"},
            "CAU_apres_4_feuilles": {"valeur": CAU_APRES_4_FEUILLES, "unite": "-", "source": "Tableau 17, Annexe 2.2.b"},
        },
        "avertissement": (
            "Calcul realise selon la methode du bilan CAU de l'Annexe 2.2.b de l'arrete "
            "referentiel regional GREN Nouvelle-Aquitaine du 29 juillet 2025 (departements "
            "incluant le 64), a partir de valeurs de reference par defaut (reliquat non "
            "mesure). Cet outil n'est pas labellise COMIFER : verifier avec votre Chambre "
            "d'agriculture sa recevabilite en cas de controle."
        ),
    }


# =========================================================================
# MIGRATIONS / TABLES
# =========================================================================

def _ensure_tables():
    with sqlite3.connect(DB_PATH) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(parcelles)").fetchall()}
        if "type_sol" not in cols:
            conn.execute("ALTER TABLE parcelles ADD COLUMN type_sol TEXT DEFAULT NULL")
        if "az_lat_ref" not in cols:
            conn.execute("ALTER TABLE parcelles ADD COLUMN az_lat_ref REAL DEFAULT NULL")
        if "az_lon_ref" not in cols:
            conn.execute("ALTER TABLE parcelles ADD COLUMN az_lon_ref REAL DEFAULT NULL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bilan_azote_mais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geofence_id INTEGER NOT NULL,
                sous_parcelle_id INTEGER,
                campagne TEXT NOT NULL,
                params_json TEXT NOT NULL,
                resultat_json TEXT NOT NULL,
                date_calcul TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bilan_azote_ble (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                geofence_id INTEGER NOT NULL,
                sous_parcelle_id INTEGER,
                campagne TEXT NOT NULL,
                params_json TEXT NOT NULL,
                resultat_json TEXT NOT NULL,
                date_calcul TEXT NOT NULL
            )
        """)
        conn.commit()


# =========================================================================
# AIDE AU PRE-REMPLISSAGE (reutilise les donnees deja en base)
# =========================================================================

def _objectif_rendement_moyenne(geofence_id, sous_parcelle_id=None):
    """
    Objectif de rendement = moyenne des rendements reels des 5 dernieres campagnes de mais
    grain sur cette parcelle, en excluant min et max (regle officielle, article 2 de
    l'arrete prefectoral). Retourne None si moins de 3 valeurs disponibles (V1 : dans ce
    cas la valeur doit etre saisie manuellement -- les references departementales
    officielles ne sont pas encore integrees ici, voir le PDF GREN "references de
    rendements" pour une saisie manuelle en attendant).
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT rendement, exit_time FROM interventions
            WHERE geofence_id = ? AND rendement IS NOT NULL
              AND intervention_type LIKE '%ecolte%'
        """
        params = [geofence_id]
        if sous_parcelle_id:
            query += " AND sous_parcelle_id = ?"
            params.append(sous_parcelle_id)
        query += " ORDER BY exit_time DESC LIMIT 5"
        rows = [r["rendement"] for r in conn.execute(query, params).fetchall()]

    if len(rows) < 3:
        return None, rows
    valeurs = sorted(rows)
    valeurs_sans_extremes = valeurs[1:-1]
    moyenne = sum(valeurs_sans_extremes) / len(valeurs_sans_extremes)
    return round(moyenne, 1), rows


def _precedent_info(geofence_id, campagne_actuelle, sous_parcelle_id=None):
    """
    Recupere le rendement et l'azote total apporte sur la campagne precedente pour cette
    parcelle, en reutilisant la logique de rattachement culture/campagne deja utilisee par
    /api/fertilisation (dashboard.find_culture_for_intervention), pour rester coherent avec
    le reste de l'application plutot que de reimplementer une regle differente.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT device_id, geofence_id, exit_time, intervention_type, applied_area,
                   products, rendement, sous_parcelle_id
            FROM interventions WHERE geofence_id = ? ORDER BY exit_time ASC
        """, (geofence_id,))
        all_interv = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name, type, teneur_n, unit, dose, culture FROM catalog_products")
        products_catalog = {r["name"].strip(): dict(r) for r in cur.fetchall()}

        dashboard._ensure_sous_parcelles_table()
        sous_parcelles_info = dashboard._get_sous_parcelles_info(conn)

    cultures_rules = dashboard.get_cultures_rules()

    rendement_precedent = None
    azote_precedent = 0.0
    for iv in all_interv:
        if sous_parcelle_id and iv.get("sous_parcelle_id") != sous_parcelle_id:
            continue
        culture_nom, campagne_label = dashboard.find_culture_for_intervention(
            iv["geofence_id"], iv["exit_time"], all_interv, cultures_rules, products_catalog,
            sous_parcelle_id=iv.get("sous_parcelle_id"), sous_parcelles_info=sous_parcelles_info
        )
        if campagne_label == campagne_actuelle:
            continue  # on cherche la campagne PRECEDENTE, pas la campagne en cours
        if iv.get("rendement"):
            rendement_precedent = iv["rendement"]
        try:
            prods = json.loads(iv.get("products") or "[]")
        except Exception:
            prods = []
        area = float(iv.get("applied_area") or 0)
        for p in prods:
            if p.get("type") != "engrais":
                continue
            catalog_entry = products_catalog.get(p.get("name", "").strip())
            teneur_n = (catalog_entry or {}).get("teneur_n") or 0
            try:
                dosage = float(p.get("dosage") or 0)
            except Exception:
                dosage = 0
            # teneur_n est un pourcentage (ex: Ammonitrate 33.5 -> teneur_n=33.5, soit 33.5%),
            # meme convention que partout ailleurs dans l'application (voir fertilisation_bp.py :
            # "n_ha = dosage * tn / 100"). Sans cette division, l'azote calcule etait jusqu'a
            # 100x trop eleve, ce qui faussait ensuite l'estimation du reliquat Ri (methode b)
            # et donc la dose previsionnelle finale.
            azote_precedent += dosage * teneur_n / 100

    return rendement_precedent, round(azote_precedent, 1)


@azote_bp.route("/api/azote/mais/pre_remplissage/<int:geofence_id>")
def azote_mais_pre_remplissage(geofence_id):
    """
    Renvoie les valeurs suggerees pour pre-remplir le calculateur : objectif de rendement
    (moyenne 5 ans hors extremes), type de sol deja enregistre pour la parcelle, rendement
    et azote apportes sur la campagne precedente. Toutes les valeurs restent modifiables
    avant le calcul final.
    """
    _ensure_tables()
    campagne = request.args.get("campagne", "")
    sous_parcelle_id = request.args.get("sous_parcelle_id", type=int)

    objectif_rendement, historique_rendements = _objectif_rendement_moyenne(geofence_id, sous_parcelle_id)
    rendement_precedent, azote_precedent = _precedent_info(geofence_id, campagne, sous_parcelle_id)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT type_sol, az_lat_ref, az_lon_ref FROM parcelles WHERE geofence_id = ?", (geofence_id,)).fetchone()
        type_sol_actuel = row["type_sol"] if row else None
        lat_ref = row["az_lat_ref"] if row else None
        lon_ref = row["az_lon_ref"] if row else None

    return jsonify({
        "objectif_rendement_suggere": objectif_rendement,
        "historique_rendements_utilises": historique_rendements,
        "type_sol_actuel": type_sol_actuel,
        "lat_ref": lat_ref,
        "lon_ref": lon_ref,
        "rendement_precedent_suggere": rendement_precedent,
        "azote_apporte_precedent_suggere": azote_precedent,
        "soil_types": [{"key": k, "label": SOIL_LABELS[k], "description": SOIL_DESCRIPTIONS[k]} for k in SOIL_TYPES],
        "precedents_disponibles": [{"key": k, "label": v["label"]} for k, v in PRECEDENTS_CAS1.items()],
    })


@azote_bp.route("/api/parcelles/<int:geofence_id>/type_sol", methods=["POST"])
def set_type_sol(geofence_id):
    """Enregistre le type de sol declare pour une parcelle (une des 8 categories GREN)."""
    _ensure_tables()
    data = request.get_json(silent=True) or {}
    type_sol = data.get("type_sol")
    if type_sol not in SOIL_TYPES:
        return jsonify({"error": f"type_sol invalide, doit etre l'un de : {SOIL_TYPES}"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE parcelles SET type_sol = ? WHERE geofence_id = ?", (type_sol, geofence_id))
        conn.commit()
    return jsonify({"status": "ok"})


@azote_bp.route("/api/parcelles/<int:geofence_id>/coords_azote", methods=["POST"])
def set_coords_azote(geofence_id):
    """
    Enregistre un point de reference (lat/lon) pour une parcelle, utilise uniquement pour
    interroger la pluviometrie historique (Open-Meteo) -- n'a aucun lien avec la geofence
    Traccar elle-meme (qui reste la seule reference pour la carte de chantier, etc.). Un
    point approximatif au centre de la parcelle suffit amplement pour une donnee meteo.
    """
    _ensure_tables()
    data = request.get_json(silent=True) or {}
    lat = data.get("lat")
    lon = data.get("lon")
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lon invalides"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE parcelles SET az_lat_ref = ?, az_lon_ref = ? WHERE geofence_id = ?",
                     (lat, lon, geofence_id))
        conn.commit()
    return jsonify({"status": "ok"})


@azote_bp.route("/api/parcelles/<int:geofence_id>/centroid_auto")
def parcelle_centroid_auto(geofence_id):
    """
    Calcule automatiquement un point de reference (lat/lon) pour une parcelle, a partir de
    la geometrie de sa geofence Traccar (centroide du polygone, ou centre si c'est un
    cercle) -- reutilise les memes fonctions que la carte de chantier
    (dashboard._parse_geofence_wkt / dashboard._geofence_centroid), pas de logique
    dupliquee. Suffisant pour la meteo historique (precision de l'ordre du km, largement
    dans la marge acceptable pour une donnee de pluviometrie).
    """
    raw_geofences = dashboard.safe_get(f"{dashboard.TRACCAR_URL}/geofences")
    if not isinstance(raw_geofences, list):
        return jsonify({"error": "Impossible de recuperer les geofences depuis Traccar"}), 502

    geofence = next((g for g in raw_geofences if str(g.get("id")) == str(geofence_id)), None)
    if not geofence:
        return jsonify({"error": f"Geofence {geofence_id} introuvable sur le serveur Traccar"}), 404

    geom = dashboard._parse_geofence_wkt(geofence.get("area"))
    if not geom:
        return jsonify({"error": "Geometrie de la geofence illisible (format WKT non reconnu)"}), 422

    lat, lon = dashboard._geofence_centroid(geom)
    return jsonify({"lat": round(lat, 5), "lon": round(lon, 5)})


@azote_bp.route("/api/azote/pluviometrie")
def azote_pluviometrie():
    """
    Calcule le cumul reel de pluie (mm) entre deux dates, via l'API archive Open-Meteo
    (meme fournisseur que le reste du dashboard pour la meteo historique), a partir de
    coordonnees lat/lon. Utilise "precipitation_sum" journalier plutot que l'agregation
    horaire deja utilisee ailleurs dans dashboard.py (api_meteo_intervention) : plus adapte
    a un cumul sur plusieurs mois, et evite de sommer des centaines de points horaires.

    Query params : lat, lon, debut (YYYY-MM-DD), fin (YYYY-MM-DD).
    """
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        debut = request.args.get("debut")
        fin = request.args.get("fin")
        datetime.strptime(debut, "%Y-%m-%d")
        datetime.strptime(fin, "%Y-%m-%d")
    except (TypeError, ValueError):
        return jsonify({"error": "Parametres lat/lon/debut/fin requis (dates au format YYYY-MM-DD)"}), 400

    try:
        r = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={"latitude": lat, "longitude": lon, "start_date": debut, "end_date": fin,
                    "daily": "precipitation_sum", "timezone": "Europe/Paris"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return jsonify({"error": f"Impossible de recuperer la meteo historique : {e}"}), 502

    daily = data.get("daily", {})
    valeurs = daily.get("precipitation_sum", []) or []
    jours_manquants = sum(1 for v in valeurs if v is None)
    cumul = sum(v for v in valeurs if v is not None)

    return jsonify({
        "pluie_cumulee_mm": round(cumul, 1),
        "nb_jours": len(valeurs),
        "jours_manquants": jours_manquants,
        "periode": {"debut": debut, "fin": fin},
    })


@azote_bp.route("/api/azote/mais/calculer", methods=["POST"])
def azote_mais_calculer():
    """Calcule (sans sauvegarder) le previsionnel d'azote a partir des parametres fournis."""
    data = request.get_json(silent=True) or {}
    try:
        resultat = calculer_dose_prev_mais(data)
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(resultat)


@azote_bp.route("/api/azote/mais/sauvegarder", methods=["POST"])
def azote_mais_sauvegarder():
    """
    Calcule ET enregistre un bilan pour une parcelle/campagne donnee -- constitue le
    document de reference a conserver/presenter en cas de controle (toutes les valeurs
    d'entree et le detail du calcul sont figes a la date d'enregistrement).
    """
    _ensure_tables()
    data = request.get_json(silent=True) or {}
    geofence_id = data.get("geofence_id")
    campagne = data.get("campagne")
    sous_parcelle_id = data.get("sous_parcelle_id")
    if not geofence_id or not campagne:
        return jsonify({"error": "geofence_id et campagne sont requis"}), 400

    try:
        resultat = calculer_dose_prev_mais(data)
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO bilan_azote_mais (geofence_id, sous_parcelle_id, campagne, params_json, resultat_json, date_calcul) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (geofence_id, sous_parcelle_id, campagne, json.dumps(data, ensure_ascii=False),
             json.dumps(resultat, ensure_ascii=False), datetime.utcnow().isoformat())
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid, "status": "ok", **resultat})


@azote_bp.route("/api/azote/mais/historique/<int:geofence_id>")
def azote_mais_historique(geofence_id):
    """Liste les bilans deja calcules et enregistres pour une parcelle."""
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, campagne, sous_parcelle_id, resultat_json, date_calcul FROM bilan_azote_mais "
            "WHERE geofence_id = ? ORDER BY date_calcul DESC", (geofence_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["resultat"] = json.loads(d.pop("resultat_json"))
        except Exception:
            d["resultat"] = None
        out.append(d)
    return jsonify(out)


@azote_bp.route("/api/azote/mais/<int:bilan_id>", methods=["DELETE"])
def azote_mais_supprimer(bilan_id):
    """Supprime un bilan enregistre."""
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bilan_azote_mais WHERE id = ?", (bilan_id,))
        conn.commit()
    return jsonify({"status": "ok"})


def _pdf_ligne_tableau(pdf, colonnes, textes, line_h=4.2, gras=False, fond=None, couleur_texte=None):
    """
    Dessine une ligne de tableau PDF avec retour a la ligne automatique dans chaque
    colonne (contrairement a pdf.cell() qui tronque silencieusement tout texte trop long
    pour tenir sur une seule ligne). Toutes les colonnes de la ligne sont mises a la meme
    hauteur (celle de la colonne qui a besoin du plus de lignes), pour un tableau propre.

    colonnes : liste de (largeur_mm, align) -- align in ('L','C','R')
    textes   : liste de chaines, une par colonne (meme longueur que colonnes)
    """
    x0, y0 = pdf.get_x(), pdf.get_y()
    pdf.set_font("Arial", "B" if gras else "", 8)
    if couleur_texte:
        pdf.set_text_color(*couleur_texte)
    else:
        pdf.set_text_color(0, 0, 0)
    if fond:
        pdf.set_fill_color(*fond)

    # 1) calcule le nombre de lignes necessaires par colonne (sans rien dessiner)
    lignes_par_colonne = []
    for (w, align), texte in zip(colonnes, textes):
        lignes = pdf.multi_cell(w, line_h, texte, border=0, align=align, split_only=True) or [""]
        lignes_par_colonne.append(lignes)
    hauteur_ligne = max(len(l) for l in lignes_par_colonne) * line_h

    # 2) dessine chaque colonne a la hauteur commune (bordure + fond sur tout le bloc,
    #    puis le texte multi-lignes par-dessus)
    x = x0
    for (w, align), lignes in zip(colonnes, lignes_par_colonne):
        pdf.rect(x, y0, w, hauteur_ligne, style="DF" if fond else "D")
        pdf.set_xy(x, y0)
        pdf.multi_cell(w, line_h, "\n".join(lignes), border=0, align=align, fill=False)
        x += w
    pdf.set_xy(x0, y0 + hauteur_ligne)


def _dessiner_bilan_mais(pdf, row, nom_parcelle, safe):
    """
    Dessine le contenu complet d'un bilan azote maïs sur la page COURANTE du pdf (l'appelant
    doit avoir deja fait pdf.add_page() avant). Factorise le contenu partage entre l'export
    d'un bilan seul (/export_pdf_azote/<id>) et l'export groupe par campagne
    (/export_pdf_campagne/<campagne>), pour ne dessiner ce contenu qu'a un seul endroit.
    """
    params = json.loads(row["params_json"])
    resultat = json.loads(row["resultat_json"])

    pdf.set_font("Arial", "B", 15)
    pdf.cell(0, 10, safe("Previsionnel de fumure azotee -- Mais grain"), ln=1, align="C")
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, safe(f"{nom_parcelle}  -  Campagne {row['campagne']}"), ln=1, align="C")
    pdf.cell(0, 6, safe(f"Calcule le {row['date_calcul'][:10]}"), ln=1, align="C")
    pdf.ln(4)

    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(120, 30, 30)
    pdf.multi_cell(0, 4.5, safe(resultat.get("avertissement", "")))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 12)
    pdf.set_fill_color(30, 41, 59)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, safe("  Resultat"), ln=1, fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "", 10)
    res = resultat["resultat"]
    lignes_resultat = [
        ("Dose avant 4 feuilles", f"{res['dose_avant_4_feuilles_kgN_ha']} kgN/ha"),
        ("Dose apres 4 feuilles", f"{res['dose_apres_4_feuilles_kgN_ha']} kgN/ha"),
        ("Dose totale previsionnelle", f"{res['dose_totale_kgN_ha']} kgN/ha"),
    ]
    if res.get("regle_plancher_appliquee"):
        lignes_resultat.append(("Regle particuliere appliquee", res["regle_plancher_appliquee"]))
    for label, val in lignes_resultat:
        pdf.set_font("Arial", "B", 10)
        pdf.cell(90, 7, safe("  " + label), border="B")
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 7, safe(val), border="B", ln=1)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 12)
    pdf.set_fill_color(30, 41, 59)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, safe("  Detail du bilan (methode COMIFER, Annexe 2.2.b)"), ln=1, fill=True)
    pdf.set_text_color(0, 0, 0)

    colonnes = [(30, "L"), (20, "R"), (78, "L"), (62, "L")]
    _pdf_ligne_tableau(pdf, colonnes, ["Terme", "Valeur", "Detail", "Source"],
                        gras=True, fond=(226, 232, 240))
    for terme, infos in resultat["detail_bilan"].items():
        val_txt = f"{infos['valeur']} {infos.get('unite','')}".strip()
        _pdf_ligne_tableau(pdf, colonnes, [
            safe(terme), safe(val_txt), safe(infos.get("formule", "")), safe(infos.get("source", ""))
        ])

    pdf.ln(6)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 7, safe("Parametres saisis"), ln=1)
    pdf.set_font("Arial", "", 9)
    champs_lisibles = {
        "objectif_rendement": "Objectif de rendement (q/ha)",
        "type_sol": "Type de sol",
        "precedent": "Precedent cultural",
        "rendement_precedent": "Rendement du precedent (q/ha ou t/ha)",
        "azote_apporte_precedent": "Azote apporte au precedent (kgN/ha)",
        "valeur_a_choix": "Conditions climatiques annee precedente",
        "pluie_cumulee_mm": "Pluie cumulee 01/10-01/05 (mm)",
        "nirr": "Irrigation (kgN/ha apporte par l'eau)",
        "xa": "Apport organique recent (kgN/ha equivalent)",
        "dose_avant_4_feuilles": "Dose prevue avant 4 feuilles (kgN/ha)",
    }
    for key, label in champs_lisibles.items():
        if key in params:
            pdf.cell(0, 5.5, safe(f"  - {label} : {params[key]}"), ln=1)

    pdf.ln(4)
    pdf.set_font("Arial", "I", 7)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 4, safe(
        "Document genere automatiquement par le Dashboard Agricole a partir de l'Annexe 2.2.b "
        "de l'arrete referentiel regional GREN Nouvelle-Aquitaine du 29 juillet 2025. "
        "Reliquat sortie hiver estime (non mesure) -- voir detail ci-dessus pour la methode "
        "de calcul retenue."
    ))


@azote_bp.route("/export_pdf_azote/<int:bilan_id>")
def export_pdf_azote(bilan_id):
    """
    Exporte un bilan azote enregistre en PDF -- document detaillant chaque terme du bilan
    avec sa valeur et sa reference reglementaire exacte (tableau/annexe), pense pour etre
    conserve comme justificatif en cas de controle. Rappelle aussi l'avertissement sur le
    caractere non labellise COMIFER de l'outil (voir en-tete du module).
    """
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM bilan_azote_mais WHERE id = ?", (bilan_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Bilan introuvable"}), 404
        parcelle = conn.execute(
            "SELECT nom_parcelle, identifiant FROM parcelles WHERE geofence_id = ?",
            (row["geofence_id"],)
        ).fetchone()

    nom_parcelle = (parcelle["nom_parcelle"] if parcelle else None) or f"Parcelle {row['geofence_id']}"

    def safe(t):
        return str(t if t is not None else '').encode('latin-1', 'replace').decode('latin-1')

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    _dessiner_bilan_mais(pdf, row, nom_parcelle, safe)

    os.makedirs("exports", exist_ok=True)
    path = os.path.join("exports", f"previsionnel_azote_mais_{bilan_id}.pdf")
    pdf.output(path)
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                      download_name=f"previsionnel_azote_mais_{nom_parcelle}_{row['campagne']}.pdf")


# =========================================================================
# ROUTES CEREALES A PAILLE (ble, orge, avoine, triticale, seigle -- Annexe 2.1.b)
# =========================================================================

@azote_bp.route("/api/azote/ble/pre_remplissage/<int:geofence_id>")
def azote_ble_pre_remplissage(geofence_id):
    """
    Equivalent de /api/azote/mais/pre_remplissage pour les cereales a paille -- reutilise
    les memes helpers (_objectif_rendement_moyenne, _precedent_info), qui ne sont pas
    specifiques a une culture.
    """
    _ensure_tables()
    campagne = request.args.get("campagne", "")
    sous_parcelle_id = request.args.get("sous_parcelle_id", type=int)

    objectif_rendement, historique_rendements = _objectif_rendement_moyenne(geofence_id, sous_parcelle_id)
    rendement_precedent, azote_precedent = _precedent_info(geofence_id, campagne, sous_parcelle_id)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT type_sol, az_lat_ref, az_lon_ref FROM parcelles WHERE geofence_id = ?", (geofence_id,)).fetchone()
        type_sol_actuel = row["type_sol"] if row else None
        lat_ref = row["az_lat_ref"] if row else None
        lon_ref = row["az_lon_ref"] if row else None

    return jsonify({
        "objectif_rendement_suggere": objectif_rendement,
        "historique_rendements_utilises": historique_rendements,
        "type_sol_actuel": type_sol_actuel,
        "lat_ref": lat_ref,
        "lon_ref": lon_ref,
        "rendement_precedent_suggere": rendement_precedent,
        "azote_apporte_precedent_suggere": azote_precedent,
        "soil_types": [{"key": k, "label": SOIL_LABELS[k], "description": SOIL_DESCRIPTIONS[k]} for k in SOIL_TYPES],
        "precedents_disponibles": [{"key": k, "label": v["label"]} for k, v in PRECEDENTS_CAS1.items()],
        "cereales_disponibles": [{"key": k, "label": v} for k, v in CEREALE_LABELS.items()],
    })


@azote_bp.route("/api/azote/ble/calculer", methods=["POST"])
def azote_ble_calculer():
    """Calcule (sans sauvegarder) le previsionnel d'azote cereale a partir des parametres fournis."""
    data = request.get_json(silent=True) or {}
    try:
        resultat = calculer_dose_prev_ble(data)
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(resultat)


@azote_bp.route("/api/azote/ble/sauvegarder", methods=["POST"])
def azote_ble_sauvegarder():
    """Calcule ET enregistre un bilan cereale pour une parcelle/campagne donnee."""
    _ensure_tables()
    data = request.get_json(silent=True) or {}
    geofence_id = data.get("geofence_id")
    campagne = data.get("campagne")
    sous_parcelle_id = data.get("sous_parcelle_id")
    if not geofence_id or not campagne:
        return jsonify({"error": "geofence_id et campagne sont requis"}), 400

    try:
        resultat = calculer_dose_prev_ble(data)
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO bilan_azote_ble (geofence_id, sous_parcelle_id, campagne, params_json, resultat_json, date_calcul) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (geofence_id, sous_parcelle_id, campagne, json.dumps(data, ensure_ascii=False),
             json.dumps(resultat, ensure_ascii=False), datetime.utcnow().isoformat())
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid, "status": "ok", **resultat})


@azote_bp.route("/api/azote/ble/historique/<int:geofence_id>")
def azote_ble_historique(geofence_id):
    """Liste les bilans cereale deja calcules et enregistres pour une parcelle."""
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, campagne, sous_parcelle_id, resultat_json, date_calcul FROM bilan_azote_ble "
            "WHERE geofence_id = ? ORDER BY date_calcul DESC", (geofence_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["resultat"] = json.loads(d.pop("resultat_json"))
        except Exception:
            d["resultat"] = None
        out.append(d)
    return jsonify(out)


@azote_bp.route("/api/azote/ble/<int:bilan_id>", methods=["DELETE"])
def azote_ble_supprimer(bilan_id):
    """Supprime un bilan cereale enregistre."""
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bilan_azote_ble WHERE id = ?", (bilan_id,))
        conn.commit()
    return jsonify({"status": "ok"})


def _dessiner_bilan_ble(pdf, row, nom_parcelle, safe):
    """Equivalent de _dessiner_bilan_mais pour les cereales a paille (Annexe 2.1.b)."""
    params = json.loads(row["params_json"])
    resultat = json.loads(row["resultat_json"])
    cereale_label = CEREALE_LABELS.get(params.get("cereale"), params.get("cereale", ""))

    pdf.set_font("Arial", "B", 15)
    pdf.cell(0, 10, safe(f"Previsionnel de fumure azotee -- {cereale_label}"), ln=1, align="C")
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, safe(f"{nom_parcelle}  -  Campagne {row['campagne']}"), ln=1, align="C")
    pdf.cell(0, 6, safe(f"Calcule le {row['date_calcul'][:10]}"), ln=1, align="C")
    pdf.ln(4)

    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(120, 30, 30)
    pdf.multi_cell(0, 4.5, safe(resultat.get("avertissement", "")))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 12)
    pdf.set_fill_color(30, 41, 59)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, safe("  Resultat"), ln=1, fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "", 10)
    res = resultat["resultat"]
    lignes_resultat = [("Dose totale previsionnelle", f"{res['dose_totale_kgN_ha']} kgN/ha")]
    if res.get("regle_plancher_appliquee"):
        lignes_resultat.append(("Regle particuliere appliquee", res["regle_plancher_appliquee"]))
    for label, val in lignes_resultat:
        pdf.set_font("Arial", "B", 10)
        pdf.cell(90, 7, safe("  " + label), border="B")
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 7, safe(val), border="B", ln=1)
    pdf.ln(4)

    pdf.set_font("Arial", "B", 12)
    pdf.set_fill_color(30, 41, 59)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, safe("  Detail du bilan (methode COMIFER, Annexe 2.1.b)"), ln=1, fill=True)
    pdf.set_text_color(0, 0, 0)

    colonnes = [(30, "L"), (20, "R"), (78, "L"), (62, "L")]
    _pdf_ligne_tableau(pdf, colonnes, ["Terme", "Valeur", "Detail", "Source"],
                        gras=True, fond=(226, 232, 240))
    for terme, infos in resultat["detail_bilan"].items():
        val_txt = f"{infos['valeur']} {infos.get('unite','')}".strip()
        _pdf_ligne_tableau(pdf, colonnes, [
            safe(terme), safe(val_txt), safe(infos.get("formule", "")), safe(infos.get("source", ""))
        ])

    pdf.ln(6)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, 7, safe("Parametres saisis"), ln=1)
    pdf.set_font("Arial", "", 9)
    champs_lisibles = {
        "cereale": "Cereale",
        "objectif_rendement": "Objectif de rendement (q/ha)",
        "type_sol": "Type de sol",
        "precedent": "Precedent cultural",
        "rendement_precedent": "Rendement du precedent (q/ha ou t/ha)",
        "azote_apporte_precedent": "Azote apporte au precedent (kgN/ha)",
        "valeur_a_choix": "Conditions climatiques annee precedente",
        "pluie_cumulee_mm": "Pluie cumulee 01/10-01/03 (mm)",
        "nirr": "Irrigation (kgN/ha apporte par l'eau)",
        "xa": "Apport organique recent (kgN/ha equivalent)",
    }
    for key, label in champs_lisibles.items():
        if key in params:
            pdf.cell(0, 5.5, safe(f"  - {label} : {params[key]}"), ln=1)

    pdf.ln(4)
    pdf.set_font("Arial", "I", 7)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 4, safe(
        "Document genere automatiquement par le Dashboard Agricole a partir de l'Annexe 2.1.b "
        "de l'arrete referentiel regional GREN Nouvelle-Aquitaine du 29 juillet 2025. "
        "Reliquat sortie hiver estime (non mesure) -- voir detail ci-dessus pour la methode "
        "de calcul retenue."
    ))


@azote_bp.route("/export_pdf_azote_ble/<int:bilan_id>")
def export_pdf_azote_ble(bilan_id):
    """Export PDF d'un bilan cereale enregistre -- meme structure que export_pdf_azote (mais)."""
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM bilan_azote_ble WHERE id = ?", (bilan_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Bilan introuvable"}), 404
        parcelle = conn.execute(
            "SELECT nom_parcelle, identifiant FROM parcelles WHERE geofence_id = ?",
            (row["geofence_id"],)
        ).fetchone()

    params = json.loads(row["params_json"])
    nom_parcelle = (parcelle["nom_parcelle"] if parcelle else None) or f"Parcelle {row['geofence_id']}"
    cereale_label = CEREALE_LABELS.get(params.get("cereale"), params.get("cereale", ""))

    def safe(t):
        return str(t if t is not None else '').encode('latin-1', 'replace').decode('latin-1')

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    _dessiner_bilan_ble(pdf, row, nom_parcelle, safe)

    os.makedirs("exports", exist_ok=True)
    path = os.path.join("exports", f"previsionnel_azote_ble_{bilan_id}.pdf")
    pdf.output(path)
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                      download_name=f"previsionnel_azote_{cereale_label}_{nom_parcelle}_{row['campagne']}.pdf")


@azote_bp.route("/export_pdf_campagne/<campagne>")
def export_pdf_campagne(campagne):
    """
    Regroupe en UN SEUL PDF tous les bilans azote (mais + cereales) enregistres pour une
    campagne donnee, un par parcelle -- pratique pour imprimer/archiver l'ensemble des
    previsionnels de l'exploitation en une fois plutot qu'un export parcelle par parcelle.

    S'il existe plusieurs bilans pour une meme parcelle (et sous-parcelle) sur cette
    campagne (plusieurs recalculs au fil de la saison), seul le PLUS RECENT est inclus --
    un export groupe doit refleter l'etat actuel, pas tout l'historique des ajustements.
    """
    _ensure_tables()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows_mais = conn.execute(
            "SELECT * FROM bilan_azote_mais WHERE campagne = ? ORDER BY date_calcul DESC",
            (campagne,)
        ).fetchall()
        rows_ble = conn.execute(
            "SELECT * FROM bilan_azote_ble WHERE campagne = ? ORDER BY date_calcul DESC",
            (campagne,)
        ).fetchall()

        def _dedupliquer_plus_recent(rows):
            vus = set()
            retenus = []
            for r in rows:
                cle = (r["geofence_id"], r["sous_parcelle_id"])
                if cle in vus:
                    continue
                vus.add(cle)
                retenus.append(r)
            return retenus

        rows_mais = _dedupliquer_plus_recent(rows_mais)
        rows_ble = _dedupliquer_plus_recent(rows_ble)

        if not rows_mais and not rows_ble:
            return jsonify({"error": f"Aucun bilan enregistre pour la campagne {campagne}"}), 404

        geofence_ids = {r["geofence_id"] for r in rows_mais} | {r["geofence_id"] for r in rows_ble}
        noms_parcelles = {}
        if geofence_ids:
            placeholders = ",".join("?" * len(geofence_ids))
            for r in conn.execute(
                f"SELECT geofence_id, nom_parcelle FROM parcelles WHERE geofence_id IN ({placeholders})",
                tuple(geofence_ids)
            ).fetchall():
                noms_parcelles[r["geofence_id"]] = r["nom_parcelle"]

        dashboard._ensure_sous_parcelles_table()
        sous_parcelles_info = dashboard._get_sous_parcelles_info(conn)

    def safe(t):
        return str(t if t is not None else '').encode('latin-1', 'replace').decode('latin-1')

    def _nom_complet(row):
        base = noms_parcelles.get(row["geofence_id"]) or f"Parcelle {row['geofence_id']}"
        sp = sous_parcelles_info.get(row["sous_parcelle_id"]) if row["sous_parcelle_id"] else None
        return f"{base} - {sp['nom']}" if sp else base

    rows_mais.sort(key=lambda r: _nom_complet(r).lower())
    rows_ble.sort(key=lambda r: _nom_complet(r).lower())

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_page()
    pdf.ln(25)
    pdf.set_font("Arial", "B", 20)
    pdf.cell(0, 12, safe("Previsionnels de fumure azotee"), ln=1, align="C")
    pdf.set_font("Arial", "", 13)
    pdf.cell(0, 8, safe(f"Campagne {campagne}"), ln=1, align="C")
    pdf.ln(6)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, safe(f"Genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')}"), ln=1, align="C")
    pdf.ln(10)
    pdf.set_font("Arial", "B", 11)
    if rows_mais:
        pdf.cell(0, 7, safe(f"  Mais grain : {len(rows_mais)} parcelle(s)"), ln=1, align="C")
    if rows_ble:
        pdf.cell(0, 7, safe(f"  Cereales a paille : {len(rows_ble)} parcelle(s)"), ln=1, align="C")
    pdf.ln(6)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(120, 30, 30)
    pdf.multi_cell(0, 4.5, safe(
        "Chaque prevision est calculee selon la methode du bilan CAU de l'arrete referentiel "
        "regional GREN Nouvelle-Aquitaine du 29 juillet 2025. Cet outil n'est pas labellise "
        "COMIFER : a verifier aupres de votre Chambre d'agriculture pour sa recevabilite en "
        "cas de controle. Seul le bilan le plus recent est repris pour chaque parcelle."
    ), align="C")
    pdf.set_text_color(0, 0, 0)

    for row in rows_mais:
        pdf.add_page()
        _dessiner_bilan_mais(pdf, row, _nom_complet(row), safe)

    for row in rows_ble:
        pdf.add_page()
        _dessiner_bilan_ble(pdf, row, _nom_complet(row), safe)

    os.makedirs("exports", exist_ok=True)
    path = os.path.join("exports", f"previsionnels_azote_campagne_{campagne}.pdf")
    pdf.output(path)
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                      download_name=f"previsionnels_azote_campagne_{campagne}.pdf")
