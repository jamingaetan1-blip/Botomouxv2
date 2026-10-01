# Image de base : Python 3.11 en version "slim" (allegee, plus rapide a
# telecharger et a construire qu'une image Python complete)
FROM python:3.11-slim
 
# Tesseract (le moteur d'OCR utilise par pytesseract) n'est PAS un paquet
# Python : c'est un programme systeme. Il faut donc l'installer via apt,
# le gestionnaire de paquets de Debian/Ubuntu (l'image "slim" est basee
# dessus). On installe aussi le pack de langue francaise, sinon
# TESS_LANG = "fra" dans main.py plantera au demarrage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*
 
# Dossier de travail a l'interieur du conteneur : tout ce qu'on fait
# ensuite (copier des fichiers, lancer des commandes) se passe ici.
WORKDIR /app
 
# On copie d'abord UNIQUEMENT requirements.txt, puis on installe les
# dependances, avant de copier le reste du code. Ca peut sembler
# bizarre, mais c'est une astuce Docker : tant que requirements.txt ne
# change pas, Docker reutilise ce qu'il a deja construit (le "cache")
# au lieu de tout reinstaller a chaque fois que tu modifies main.py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# Maintenant on copie le reste du projet (main.py, etc.)
COPY . .

# Commande executee quand le conteneur demarre
CMD ["python", "main.py"]
 
