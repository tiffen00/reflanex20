# Template de campagne — AR24

Page d'inscription clone AR24 prête à utiliser comme campagne marketing.

> ⚠️ **Campagne protégée** : cette campagne est automatiquement créée au démarrage du service et **ne peut pas être supprimée** depuis le bot Telegram. Elle sert de point de départ permanent pour la génération de liens AR24.

## Utilisation

Cette campagne est seeded automatiquement au boot — aucune action manuelle n'est requise.

Pour générer un lien AR24, utilise le bot Telegram : `📋 Mes campagnes → ar24 → 🔗 Nouveau lien`.

### Uploader manuellement (si nécessaire)

1. Zipper ce dossier :
   ```bash
   cd examples && zip -r ar24-template.zip ar24-template/
   ```
2. Uploader le zip via l'interface web Reflanex20 (onglet **Nouvelle campagne**)
   ou via le bot Telegram (envoyer le zip avec `ar24` en légende)

## Personnalisation

Éditer `index.html` pour changer les textes, le logo, les formulaires.
Éditer `style.css` pour changer les couleurs (variables CSS en haut du fichier).

## Contenu

| Fichier | Rôle |
|---|---|
| `index.html` | Page d'inscription AR24 — responsive web + mobile, sans dépendance externe |
| `style.css` | Styles AR24 (variables CSS, mobile-first, touch-friendly, WCAG AA) |

## Responsive design

La page est conçue pour fonctionner sur tous les appareils :

- 📱 Mobile (≤480px) : plein écran, inputs sans zoom iOS (font-size ≥ 16px)
- 📱 Tablette (≤540px) : carte centrée
- 💻 Desktop : carte centrée avec ombre, max-width 460px
- Touch targets ≥ 44px (iOS HIG)
- Pas de dépendance externe (Google Fonts, CDN…) → chargement < 1s
