# 🚜 Dashboard Agricole v15.6

Une application web qui tourne en local sur un NAS ou PC, et qui transforme les données GPS de **Traccar** (suivi des tracteurs et outils) en **carnet d'interventions agricoles** enrichi.

---

## Comment ça fonctionne

```
Tracteur GPS → Traccar → Dashboard Agricole → Navigateur web
```

1. Les tracteurs envoient leur position GPS à un serveur **Traccar**
2. Le Dashboard récupère automatiquement les **entrées/sorties de parcelles** (géofences)
3. L'agriculteur complète chaque passage : type d'intervention, produits, météo
4. Les données sont stockées localement dans une base **SQLite**
5. On accède à tout depuis un **navigateur web** (PC, tablette, mobile)

---

## Fonctionnalités

### 📍 Suivi GPS en temps réel
- Carte interactive avec position des véhicules
- Détection automatique des entrées/sorties de parcelles
- Historique des passages sur période configurable

### 🗺️ Carte de Chantier (RTK)
- Reconstruction de la surface réellement travaillée (largeur d'outil, pas une simple trace GPS filiforme)
- Fonds de carte Satellite / NDVI (Sentinel-2, via Copernicus Data Space ou Sentinel Hub) / Plan
- Historique NDVI numérique par parcelle et sous-parcelle, avec sparkline et repères phénologiques
- Alertes automatiques : vitesse anormale au travail, arrêt prolongé outil engagé, couverture de parcelle insuffisante
- Couverture de parcelle journalière ou cumulée sur une période
- Mode suivi live (rechargement automatique toutes les 30s)
- Comparaison de deux dates côte à côte
- Résumé de période exportable en Excel
- Exports PDF (rapport de chantier), GeoJSON, Shapefile, KML
- Saisie rapide d'intervention pré-remplie depuis la carte
- Points d'observation terrain géolocalisés (adventices, maladie/ravageur, panne, zone humide...)
- Modèles d'intervention réutilisables (produits/doses habituels en 2 clics)

### 📋 Carnet phytosanitaire
- Saisie des interventions depuis les passages Traccar détectés
- Catalogue de produits phyto avec doses homologuées
- Calcul automatique de l'**IFT** (méthode MASA officielle)
- Météo historique à la date de l'intervention (API Open-Meteo, gratuite)
- Export registre phyto au format **XML réglementaire**
- **Cahier de traçabilité** PDF : historique complet par parcelle/sous-parcelle, tous types d'intervention confondus

### ✏️ Saisie manuelle
- Interventions non détectées par GPS
- Multi-parcelles en une saisie
- Durée enregistrée pour les calculs ha/h

### 🗂️ Gestion des parcelles
- Nom, identifiant export, **surface cultivée cadastrale**
- La surface cadastrale est la référence pour l'IFT et les exports
- Statut cultural (en culture, jachère…)
- **Sous-parcelles** : scinder une parcelle en plusieurs cultures pour une campagne donnée, sans jamais toucher à la géofence Traccar elle-même
- Import de contours au format KML / GeoJSON
- Export du rapport de chantier (Excel / PDF)

### 🛰️ Import Traccar
- Assistant de création/liaison en masse des véhicules et parcelles sur le serveur Traccar
- Import de véhicules par CSV ou saisie manuelle, avec détection de conflits
- Import de parcelles par KML/GeoJSON
- **Import de parcelles/îlots depuis le RPG (IGN)** : carte interactive avec recherche d'adresse, affichage des parcelles PAC déclarées ou des îlots (contour stable), création de géofence en un clic
- **Liaisons véhicule ↔ parcelle interactives** : matrice cliquable pour lier ou délier un véhicule existant et une parcelle existante en un clic, sans repasser par un import complet
- Gestion des parcelles présentes sur le serveur (suppression directe)
- Historique des imports persisté côté serveur

### 🌿 Cahier de fertilisation
- Suivi des apports N/P/K par parcelle et par campagne
- Objectifs par culture avec taux de couverture
- Suivi de stock produit
- Gestion de campagnes (nouvelle campagne, filtre par campagne)
- Graphiques de suivi et comparaison aux objectifs
- **Prévisionnel de fumure azotée** (maïs grain et céréales à paille) selon la méthode du bilan CAU de l'arrêté GREN Nouvelle-Aquitaine, avec pluviométrie et géolocalisation automatiques, export PDF individuel ou groupé par campagne
- Export **PDF** par parcelle

### 📈 Module Analytique
- Performances par tracteur, outil, type d'intervention
- Calcul **ha/h** quand la durée est renseignée
- Carte thermique des passages (parcelle × mois)
- Évolution mensuelle
- Export **PDF** rapport complet

### 📊 Synthèse de campagne
- Vue de synthèse globale, partageable via lien à expiration configurable (lecture seule, sans compte)

### 🌿 Intégration e-phy (ANSES)
- Recherche dans le référentiel officiel des produits phytosanitaires (data.gouv.fr, Licence Ouverte)
- Synchronisation hebdomadaire automatique, ou manuelle depuis l'interface
- Pré-remplissage du catalogue produits (AMM, dose homologuée, DAR, DRE, BBCH, culture, cible)
- Détection des AMM retirées dans le catalogue produits utilisé
- Noms commerciaux secondaires indexés (un même produit vendu sous plusieurs noms remonte sous chacun d'eux dans la recherche)

### ⚙️ Système
- Paramétrage Traccar directement depuis l'interface (URL, identifiants) sans redémarrage
- Alerte visuelle si Traccar est inaccessible
- Sauvegardes automatiques de la base toutes les 24h
- Export / import de la configuration métier (catalogue, outils, cultures, parcelles, exploitation) au format JSON
- Gestion du mot de passe : hachage PBKDF2-HMAC-SHA256, alerte si mot de passe par défaut jamais changé, récupération par suppression de fichier
- **Alertes email automatiques (SMTP)** : Traccar inaccessible, point d'observation "maladie/ravageur" ou "panne" signalé — envoyées par le serveur sans action de l'utilisateur
- **Prévisions météo** (modèle officiel Météo-France ARPEGE Europe, 4 jours) affichées sur le tableau de bord
- **Export calendrier (.ics)** des interventions, importable dans Google Calendar / Outlook / Apple Calendar

### 📤 Partage
- Partage d'un point d'observation, d'une parcelle, ou d'une fiche d'intervention par **email, SMS ou WhatsApp** (un seul bouton, choix du canal au clic)
- Partage groupé de plusieurs points d'observation sélectionnés en un seul message
- Modèle de message personnalisable (en-tête et signature ajoutés automatiquement)

### ❓ Aide
- Notice d'utilisation complète intégrée

---

## Installation

### NAS Synology / Linux

**Prérequis**
```bash
pip3 install flask requests openpyxl fpdf2
```

**Lancement**

⚠️ Toujours démarrer via `run.py`, jamais `dashboard.py` directement — plusieurs blueprints font `import dashboard` pour accéder à son état partagé (config Traccar rechargeable à chaud, etc.), et Python n'accepte cet import que si le fichier a été chargé sous le nom `dashboard` (ce qui n'est pas le cas si on l'exécute directement comme script principal, provoquant une erreur d'import circulaire).

```bash
cd /chemin/vers/Traccar_dashboard
python3 run.py
```

Accès sur : `http://[IP-du-serveur]:8080`

Scripts fournis :
- `start.sh` : démarrage en arrière-plan avec log dans `app.log`
- `restart.sh` : arrêt puis redémarrage (cible le process `python3 run.py`)

### Windows

Dézipper l'archive Windows puis cliquer sur le fichier installer.bat pour lancer l'installateur pour générer l'exécutable.

L'installateur :
- Télécharge et installe **Python 3.12** automatiquement si absent
- Copie les fichiers du dashboard
- Installe les dépendances pip
- Crée un **raccourci sur le bureau**
- Ouvre le navigateur automatiquement au premier lancement
- Login / mot de passe : admin / admin

---

## Structure des fichiers

```
Traccar_dashboard/
├── run.py                     # Point d'entrée à utiliser pour lancer le serveur
├── dashboard.py                # Application principale Flask (noyau + état partagé)
├── interventions.py            # Blueprint : interventions, parcelles, catalogue, sous-parcelles, exploitation
├── ndvi_bp.py                   # Blueprint : NDVI Sentinel-2 (carte + historique par parcelle)
├── ephy_bp.py                    # Blueprint : référentiel e-phy ANSES (autonome, tables dédiées)
├── templates_bp.py                # Blueprint : modèles d'intervention réutilisables
├── share_tokens_bp.py              # Blueprint : liens de partage en lecture seule (Synthèse)
├── field_points_bp.py               # Blueprint : points d'observation terrain + sous-parcelles
├── cahier_bp.py                       # Blueprint : cahier de traçabilité (export PDF)
├── chantier_export_bp.py               # Blueprint : exports du rapport de chantier (Excel/PDF)
├── analytique_bp.py                     # Blueprint : module analytique
├── traccar_import_bp.py                  # Blueprint : assistant d'import Traccar + proxy + liaisons
├── config_backup_bp.py                    # Blueprint : export/import de la config métier (JSON)
├── fertilisation_bp.py                     # Blueprint : cahier de fertilisation
├── azote_bp.py                               # Blueprint : prévisionnel de fumure azotée (GREN)
├── backup.py                                # Sauvegarde quotidienne automatique de la base (sans route Flask)
├── database.db                               # Base de données SQLite (créée automatiquement)
├── config.json                                # Configuration Traccar / alertes / NDVI (créé via l'interface)
├── secret_key.txt                              # Clé secrète Flask (session), générée au premier démarrage
├── password_override.txt                       # Mot de passe admin haché (PBKDF2-HMAC-SHA256)
├── templates/                                    # Vues HTML
│   ├── index.html                                 # Dashboard principal
│   ├── chantier.html                              # Carte de chantier RTK
│   ├── synthese.html                              # Synthèse de campagne
│   ├── traccar_import.html                        # Assistant d'import Traccar
│   ├── analytique.html                            # Module analytique
│   ├── fertilisation.html                         # Cahier de fertilisation
│   ├── Notice.html                                # Aide / notice intégrée
│   ├── login.html                                 # Page de connexion
│   └── change_password.html
├── exports/                                        # Fichiers PDF et Excel générés (créé automatiquement)
├── backups/                                        # Sauvegardes automatiques de la DB (créé automatiquement)
├── start.sh                                        # Démarrage (Linux/NAS)
└── restart.sh                                      # Redémarrage (Linux/NAS)
```

---

## Premier lancement

1. Ouvrir `http://[IP]:8080`
2. Se connecter (mot de passe défini au premier démarrage) : par défaut admin /admin 
3. Aller dans **⚙️ État système → Paramétrage Traccar**
4. Renseigner l'URL, l'identifiant et le mot de passe Traccar
5. Cliquer **💾 Enregistrer**

---

## Stack technique

| Composant | Technologie |
|---|---|
| Serveur | Python 3.9+ · Flask |
| Base de données | SQLite |
| Carte | Leaflet.js |
| Graphiques | Chart.js |
| Météo | Open-Meteo API (gratuite, sans clé) |
| NDVI | Sentinel-2 via Copernicus Data Space Ecosystem / Sentinel Hub |
| Référentiel produits | e-phy ANSES (data.gouv.fr, Licence Ouverte) |
| Export PDF | fpdf2 |
| Export Excel | openpyxl |
| GPS | Traccar API |

---

## Historique des versions

| Version | Nouveautés principales |
|---|---|
| **15.6** | Partage par email/SMS/WhatsApp (points d'observation, parcelles, fiches d'intervention), avec modèle de message personnalisable · Prévisions météo (Météo-France ARPEGE Europe, 4 jours) sur le tableau de bord · Alertes email automatiques (SMTP) : Traccar inaccessible, points terrain sensibles · Export calendrier (.ics) |
| **15.5** | Prévisionnel de fumure azotée (maïs et céréales à paille, méthode du bilan CAU, arrêté GREN Nouvelle-Aquitaine du 29 juillet 2025) avec pluviométrie et géolocalisation automatiques · Import de parcelles/îlots depuis le RPG (IGN) via carte interactive dans l'assistant d'import Traccar |
| **15.4** | Liaisons véhicule ↔ parcelle interactives (lier/délier au clic dans l'assistant d'import Traccar) · Correctif e-phy : noms commerciaux secondaires désormais indexés dans la recherche |
| **15.3** | Historique NDVI numérique par parcelle/sous-parcelle · Import KML/GeoJSON · Mot de passe haché (PBKDF2-HMAC-SHA256) · Alerte mot de passe par défaut · Option cookie de session sécurisé (HTTPS) |
| **15.0** | Sous-parcelles · Rendement à la récolte · Filtre par campagne · Stock produit · Nouvelle campagne · Saisie rapide d'intervention · Points d'observation terrain · Modèles d'intervention |
| **14.2** | Réorganisation des icônes par fonction · Assistant d'import Traccar (véhicules/parcelles/liaisons) · Alerte arrêt prolongé outil engagé · Mode cumulé de couverture · Recherche dans le Carnet et le catalogue |
| **12.5** | Intégration e-phy (ANSES) : recherche et pré-remplissage du catalogue produits |
| **12.3** | Alerte Traccar inaccessible · Bouton Aide · Installateur Windows auto-Python |
| **12.2** | Fix calcul IFT (méthode MASA) · Fix déclaration JS |
| **12.1** | Colonne Surface cultivée dans tableau · Fix cache parcelles |
| **12.0** | Paramétrage Traccar via UI · Surface cadastrale · Météo historique · Export PDF · Filtre 7j |
| **11.x** | Versions initiales |

---

## Intégration e-phy (ANSES)

### Référentiel produits phytosanitaires

Le catalogue produits est connecté à la base **e-phy de l'ANSES** (données officielles, Licence Ouverte, mises à jour hebdomadaires).

#### Comment ça marche

- Au démarrage du serveur, une **synchronisation automatique** est lancée si la base locale a plus de 7 jours
- La base e-phy (~5 000 produits réellement autorisés, noms commerciaux principaux et secondaires confondus, ~19 000 usages) est stockée localement dans la base SQLite — la recherche est **instantanée**, même sans internet
- Le bouton **🌿 e-phy** dans le formulaire catalogue permet de rechercher et pré-remplir un produit

#### Utilisation

1. Ouvrir ⚙️ Catalogue produits
2. Cliquer **🌿 e-phy**
3. Taper le nom du produit (ex: Roundup, Amistar, Elumis...) — les noms commerciaux secondaires d'un même produit remontent également
4. Cliquer sur le produit dans les résultats
5. Le formulaire se pré-remplit automatiquement :
   - Nom, AMM, fonction
   - Dose homologuée + unité
   - DAR (délai avant récolte)
   - DRE (délai de rentrée, en heures)
   - BBCH min/max (stades culturaux)
   - Culture et cible de traitement
6. Compléter la dose conseillée et enregistrer

#### Synchronisation manuelle

Depuis **⚙️ État système** → bouton **📥 Télécharger** ou **🔄 Mettre à jour**.

La synchronisation télécharge ~4 Mo depuis data.gouv.fr et prend 1-2 minutes.

#### Source des données

Données E-Phy — Anses — [data.gouv.fr](https://www.data.gouv.fr) — Licence Ouverte
