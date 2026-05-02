# Reflanex20 — Campaign Link Manager

Système web + bot Telegram pour gérer des campagnes marketing : upload de zips, génération de liens uniques à la volée, multi-domaines, stats de clics. Déployable en 5 minutes sur **Render**.

---

## 1. Présentation

En marketing, chaque campagne génère un lien qui finit par être blacklisté ou cramé. Reflanex20 résout ce problème : tu uploades ton zip **une seule fois**, puis tu génères autant de nouveaux slugs que tu veux en une commande, sans re-uploader quoi que ce soit.

---

## 2. Fonctionnalités

- 📤 **Upload** de zips via interface web (drag & drop) ou bot Telegram
- 🎲 **Génération de slugs** aléatoires (8 chars, anti-confusion) pointant vers la même campagne
- 🌐 **Multi-domaines** : associe n'importe quel domaine à un lien
- 📊 **Stats** : compteur de clics par lien
- 🤖 **Bot Telegram** : toutes les opérations sans ouvrir le navigateur
- 🔐 **Auth** : API token + whitelist d'IDs Telegram

---

## 3. Architecture

```
┌──────────────────┐     ┌──────────────────┐
│  Interface Web   │     │  Bot Telegram    │
│  (drag & drop)   │     │  /newlink, /list │
└────────┬─────────┘     └────────┬─────────┘
         │                        │
         └──────────┬─────────────┘
                    ▼
         ┌──────────────────────────────┐
         │     FastAPI (backend/)       │
         │  - POST /api/upload          │
         │  - POST /api/campaigns/:id/links │
         │  - GET  /c/{slug}/           │
         │  - SQLite (SQLAlchemy)       │
         └──────────┬───────────────────┘
                    │
                    ▼
         ┌──────────────────────────────┐
         │  storage/campaigns/          │
         │  <slug-dir>/index.html ...   │
         └──────────────────────────────┘
```

---

## 4. Setup local

```bash
# 1. Clone
git clone https://github.com/tiffen00/reflanex20.git
cd reflanex20

# 2. Environnement virtuel
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Dépendances
pip install -r requirements.txt

# 4. Configuration
cp .env.example .env
# Édite .env : ajoute ton TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_IDS, DOMAINS, etc.

# 5. Lancer
bash start.sh
# ou directement :
uvicorn backend.main:app --reload
```

Ouvre http://localhost:8000 — entre ton `API_TOKEN` (affiché dans les logs au 1er démarrage si vide).

---

## 5. Déploiement sur Render

1. **Push** ce repo sur GitHub (ou fork)
2. Va sur https://render.com → **New Web Service**
3. Connecte ton repo GitHub → Render détecte automatiquement `render.yaml`
4. Dans **Environment**, renseigne :
   - `TELEGRAM_BOT_TOKEN` — token donné par @BotFather
   - `TELEGRAM_ADMIN_IDS` — ton ID Telegram (voir @userinfobot)
   - `DOMAINS` — tes domaines séparés par des virgules
   - `PUBLIC_BASE_URL` — `https://<ton-service>.onrender.com`
5. `API_TOKEN` est **auto-généré** par Render (`generateValue: true`) — retrouve-le dans le dashboard Render → ton service → **Environment** → `API_TOKEN`
6. Clique **Deploy** — Render installe les deps, monte le disque `/opt/render/project/src/storage`, et démarre avec `bash start.sh`

---

## 6. Créer le bot Telegram

1. **@BotFather** → `/newbot` → donne un nom et un username → copie le **token** (`123456:ABC-...`)
2. **@userinfobot** → envoie n'importe quel message → copie ton **User ID** (nombre)
3. Mets ces valeurs dans les vars d'env Render (ou `.env` en local)

---

## 7. Config domaines

Pour chaque domaine que tu veux utiliser :

1. **Render Dashboard** → ton service → **Custom Domains** → ajoute le domaine
2. Chez ton **registrar** : crée un enregistrement CNAME :
   ```
   CNAME  @  <ton-service>.onrender.com
   ```
   (ou utilise Cloudflare en mode proxy pour encore plus de vitesse)
3. Ajoute le domaine à la var `DOMAINS` dans Render (ex: `promo1.com,offer-x.net`)
4. Redémarre le service

---

## 8. Utilisation

### Via l'interface web

| Action | Comment |
|---|---|
| Upload une campagne | Onglet **Upload** → nom + glisse le zip → **Uploader** |
| Voir les campagnes | Onglet **Campagnes** |
| Générer un lien | Clic **🔗 Liens** → choisir domaine → **+ Nouveau lien** |
| Copier un lien | Bouton 📋 |
| Désactiver un lien | Bouton **Désactiver** |

### Via le bot Telegram

```
/upload                      → envoie ensuite le zip avec le nom en légende
/list                        → liste les campagnes (id, nom, nb liens)
/newlink 3                   → génère un lien pour la campagne 3 (clavier inline de domaines)
/newlink 3 promo1.com        → génère directement avec ce domaine
/setdomain abc12345 promo2.com → change le domaine d'un lien existant
/delete abc12345             → désactive le lien
/stats 3                     → stats (liens + clics) pour la campagne 3
/domains                     → liste les domaines configurés
```

---

## 9. Avertissement

Ce projet est conçu pour des campagnes marketing **légitimes** : landing pages, A/B testing, rotations promotionnelles, redirections de liens. L'utilisateur est seul responsable du contenu hébergé et de la conformité avec les législations en vigueur (RGPD, anti-spam, etc.).
