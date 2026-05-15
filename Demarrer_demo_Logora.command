#!/bin/bash
# ============================================================
# Lanceur double-clic pour la démo Logora
# Double-clique ce fichier dans le Finder.
# Le Terminal s'ouvre, le service démarre, et le navigateur
# s'ouvre automatiquement sur http://localhost:8000
# ============================================================

cd "$(dirname "$0")"

echo ""
echo "  ════════════════════════════════════════════════════════"
echo "    Logora — Démarrage de la démo de modération"
echo "  ════════════════════════════════════════════════════════"
echo ""
echo "  Dossier : $(pwd)"
echo ""

# 1. Vérifier que Python 3 est installé
if command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
else
    echo "  ❌ Python 3 introuvable sur ce Mac."
    echo "     Installe-le depuis https://www.python.org/downloads/"
    echo ""
    read -p "  Appuie sur Entrée pour fermer..."
    exit 1
fi
echo "  Python détecté : $($PYTHON --version 2>&1)"

# 2. Vérifier que serve.py et le HTML sont bien là
if [ ! -f "serve.py" ]; then
    echo "  ❌ serve.py introuvable dans ce dossier."
    echo ""
    read -p "  Appuie sur Entrée pour fermer..."
    exit 1
fi
if [ ! -f "moderation_lequipe_demo.html" ]; then
    echo "  ❌ moderation_lequipe_demo.html introuvable."
    echo ""
    read -p "  Appuie sur Entrée pour fermer..."
    exit 1
fi

# 3. Vérifier que le port 8000 est libre
if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  ⚠ Le port 8000 est déjà utilisé par un autre process."
    echo "     Je tente de le libérer..."
    lsof -ti :8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null
    sleep 1
fi

# 4. Ouvrir le navigateur automatiquement après que le serveur démarre
( sleep 2 && open "http://localhost:8000" ) &

# 5. Lancer le serveur (bloquant — Ctrl+C pour arrêter)
echo ""
echo "  ⏳ Démarrage du serveur..."
echo "  (le navigateur s'ouvre automatiquement dans 2 secondes)"
echo ""
exec $PYTHON serve.py
