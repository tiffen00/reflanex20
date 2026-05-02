# Template de campagne — AR24-style

Page d'inscription clone AR24 prête à utiliser comme campagne marketing.

## Utilisation

1. Zipper ce dossier :
   ```bash
   cd examples && zip -r ar24-template.zip ar24-template/
   ```
2. Uploader le zip via l'interface web Reflanex20 (onglet **Nouvelle campagne**)
   ou via le bot Telegram (`/upload` puis envoyer le zip avec le nom en légende)
3. Générer un lien et le partager

## Personnalisation

Éditer `index.html` pour changer les textes, le logo, les formulaires.
Éditer `style.css` pour changer les couleurs (variables CSS en haut du fichier).

## Contenu

| Fichier | Rôle |
|---|---|
| `index.html` | Page d'inscription AR24 (formulaire email + password + CGU) |
| `style.css` | Styles AR24 (fond clair, cards blanches, boutons pill bleu/violet) |
