"""
Sauvegarde automatique de la base de donnees SQLite : copie quotidienne dans backups/, avec
purge des sauvegardes de plus de 30 jours.

Extrait de dashboard.py -- module utilitaire pur, sans route Flask (pas de blueprint ici,
juste des fonctions appelees au demarrage et par un thread dedie). Aucune dependance externe.
"""
import os
import time
import shutil
from datetime import datetime, timedelta


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
