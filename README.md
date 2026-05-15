# Logora × L'Équipe — Démo de modération

Démo interactive de modération de commentaires basée sur la charte V2 de L'Équipe.
Backend Python (stdlib uniquement) + HTML/JS autonome.

## Lancer en local

1. Crée un fichier `config.py` à la racine (à partir de `config.example.py`) avec ta clé API Anthropic.
2. Démarre le serveur :

   ```bash
   python3 serve.py
   ```

3. Ouvre `http://localhost:8000` dans ton navigateur.

Sur Mac, tu peux aussi double-cliquer `Demarrer_demo_Logora.command`.

## Déployer publiquement sur Render (URL publique en 5 min)

### Étape 1 — Pousser sur GitHub

Crée un repo **privé** sur github.com (clic sur **+** → **New repository** → coche "Private"), puis depuis ce dossier :

```bash
git init
git add .
git commit -m "Initial commit — Logora moderation demo"
git branch -M main
git remote add origin https://github.com/<TON_USER>/<TON_REPO>.git
git push -u origin main
```

Le `.gitignore` exclut automatiquement `config.py` et `corrections.json` — ta clé API ne sera **pas** publiée.

### Étape 2 — Déployer sur Render

1. Va sur [render.com](https://render.com) et crée un compte (login GitHub disponible).
2. Clic sur **New +** → **Web Service** → **Build and deploy from a Git repository**.
3. Connecte ton compte GitHub si demandé, sélectionne ton repo.
4. Render détecte automatiquement `render.yaml`. Tous les paramètres sont préremplis.
5. Avant de cliquer Deploy, va dans l'onglet **Environment** et ajoute la variable :

   - **Key** : `ANTHROPIC_API_KEY`
   - **Value** : `sk-ant-api03-...` (ta vraie clé)

6. Clic sur **Create Web Service**. Le build prend ~30 s.
7. Render te donne une URL publique du style `https://logora-moderation-demo.onrender.com`. Partage-la à L'Équipe.

### Limites du free tier Render

- **Sleep** : si personne n'utilise l'app pendant 15 min, l'instance s'éteint. Le prochain visiteur attendra ~30 s au cold start.
- **Filesystem éphémère** : `corrections.json` est perdu à chaque redémarrage / redéploiement. Pour de la persistance, passe au plan **Starter** (7 $/mois) avec un disque persistant.
- **Bande passante** : 100 GB/mois, largement suffisant pour la démo.

### Recommandations Anthropic

- Ton compte est actuellement en **tier 0** (5 modérations/min). Pour fluidifier les démos en batch, ajoute 5 $ de crédit dans `console.anthropic.com → Billing` pour passer en **tier 1** (50 modérations/min).
- Mets un **plafond de dépense** sur la clé (Settings → Limits) pour éviter les surprises si la démo devient publique.
- Une fois le test L'Équipe terminé, **révoque cette clé** et crée-en une autre pour la production.

## Architecture

```
moderation_lequipe_demo.html   # UI : appelle /api/moderate, /api/feedback
serve.py                        # Proxy : injecte la charte + corrections, sanitise
config.py                       # Clé API locale (gitignored)
corrections.json                # Ajustements rédac (gitignored)
.gitignore                      # Protège les secrets
requirements.txt                # Aucune dépendance externe
Procfile                        # Pour les hébergeurs Heroku-style
render.yaml                     # Config Render automatique
```

## Sécurité

- **La clé API n'est jamais envoyée au navigateur.** Toutes les requêtes passent par `serve.py` qui ajoute la clé côté serveur.
- **Toutes les références au fournisseur sous-jacent sont sanitisées** dans les réponses d'erreur (regex côté serveur).
- **CORS** : les endpoints renvoient `Access-Control-Allow-Origin: *` — restreins si tu héberges l'UI séparément.
