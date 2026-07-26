"""
Point d'entree pour lancer le serveur -- utiliser :

    python run.py

au lieu de "python dashboard.py" directement.

Pourquoi : les blueprints du dashboard (ndvi_bp.py notamment) font "import dashboard" pour
acceder a son etat partage (TRACCAR_URL, etc.) et le voir toujours a jour, y compris apres un
rechargement de configuration en cours de fonctionnement. Cela ne fonctionne correctement que
si "dashboard" est le nom sous lequel Python a charge ce fichier -- or lancer un script
directement avec "python dashboard.py" le charge sous le nom "__main__", pas "dashboard".
Dans ce cas, "import dashboard" depuis un blueprint declenche une SECONDE execution complete
du fichier sous une identite de module differente, provoquant une erreur d'import circulaire.

En passant par ce petit fichier séparé, dashboard.py est toujours importé normalement (jamais
exécuté comme script principal), ce qui évite complètement le problème.
"""
from dashboard import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=8080)
