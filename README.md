# 🚜 Dashboard Agricole

Une application web qui tourne en local sur un NAS ou PC, et qui transforme les données d'**AgOpenGPS (Urepom design)** envoyées sur **Traccar** (suivi des tracteurs et outils) en **carnet d'interventions agricoles** enrichi.


---

## Comment ça fonctionne

AgOpenGPS Urepom Design est téléchargeable ici : https://github.com/Urepom/AgOpenGPS_Urepom_Design

Traccar server est disponible ici : https://www.traccar.org/download/

```
AgOpenGPS Urepom Design → Traccar → Dashboard Agricole → Navigateur web
```

1. **AgOpenGPS Urepom Design** envoi la position GPS position GPS des tracteurs à un serveur **Traccar**
2. Le Dashboard récupère automatiquement les **entrées/sorties de parcelles** (périmètres virtuels)
3. L'agriculteur complète chaque passage : type d'intervention, produits, météo
4. Les données sont stockées localement dans une base **SQLite**
5. On accède à tout depuis un **navigateur web** (PC, tablette, mobile)

---

## Fonctionnalités

### 📍 Suivi GPS en temps réel
- Carte interactive avec position des véhicules
- Détection automatique des entrées/sorties de parcelles
- Historique des passages sur 7 jours (configurable)

### 📋 Carnet phytosanitaire
- Saisie des interventions depuis les passages Traccar détectés
- Catalogue de produits phyto avec doses homologuées
- Calcul automatique de l'**IFT**
- Météo historique à la date de l'intervention (API Open-Meteo, gratuite)
- Export registre phyto au format **XML réglementaire**

### ✏️ Saisie manuelle
- Interventions non détectées par GPS
- Multi-parcelles en une saisie
- Durée enregistrée pour les calculs ha/h

### 🗂️ Gestion des parcelles
- Nom, identifiant export, **surface cultivée**
- La surface cultivée est la référence pour l'IFT et les exports
- Statut cultural (en culture, jachère…)

### 🌿 Cahier de fertilisation
- Suivi des apports N/P/K par parcelle et par campagne
- Objectifs par culture avec taux de couverture
- Graphiques de suivi et comparaison aux objectifs
- Export **PDF** par parcelle

### 📈 Analytique
- Performances par tracteur, outil, type d'intervention
- Calcul **ha/h** quand la durée est renseignée
- Carte thermique des passages (parcelle × mois)
- Évolution mensuelle
- Export **PDF** rapport complet

### ⚙️ Système
- Paramétrage Traccar directement depuis l'interface (URL, identifiants) sans redémarrage
- Alerte visuelle si Traccar est inaccessible
- Sauvegardes automatiques de la base toutes les 24h
- Gestion du mot de passe

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
```bash
cd /chemin/vers/Traccar_dashboard
python3 dashboard.py
```

Accès sur : `http://[IP-du-serveur]:8080`

### Windows

Lancer`DashboardAgricole_setup_vXXX.exe` puis suivez les étapes de l'installateur.

L'installateur :
- Télécharge et installe **Python 3.12** automatiquement si absent
- Copie les fichiers du dashboard
- Installe les dépendances pip
- Crée un **raccourci sur le bureau**
- Ouvre le navigateur automatiquement au premier lancement

---

## Structure des fichiers

```
Traccar_dashboard/
├── dashboard.py          # Application principale Flask
├── interventions.py      # API interventions, parcelles, catalogue
├── database.db           # Base SQLite (créée automatiquement)
├── config.json           # Configuration Traccar (créé via l'interface)
├── templates/
│   ├── index.html        # Dashboard principal
│   ├── fertilisation.html
│   ├── analytique.html
│   ├── Notice.html       # Aide
│   ├── login.html
│   └── change_password.html
├── backups/              # Sauvegardes automatiques
├── exports/              # PDF et XML générés
└── restart.sh            # Redémarrage (Linux/NAS)
```

---

## Premier lancement

1. Ouvrir `http://[IP]:8080`
2. Se connecter : utilisateur : admin / mot de passe : changeme 
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
| Export PDF | fpdf2 |
| Export Excel | openpyxl |
| GPS | Traccar API |

---


