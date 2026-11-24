# frigo

La porte du frigo, version web : la liste d'épicerie, les tâches de la maison et les gros
achats à planifier, pour deux personnes. Claude peut y ajouter des choses par un serveur MCP
intégré.

## Épicerie

- Ajout en texte libre : « lait 2 L », « 3 oignons », « 1 lb bœuf haché #lasagne ». La
  quantité et les étiquettes (`#recette`) sont reconnues ; `_` devient une espace dans une
  étiquette.
- « Coller une liste » ajoute une ligne par article, avec une étiquette commune facultative.
- Un article déjà à acheter n'est jamais dupliqué : la quantité s'ajoute (« 2 + 1 ») et les
  étiquettes se cumulent.
- La liste est groupée par rayon, dans l'ordre où on parcourt l'épicerie (Fruits et légumes
  d'abord, Autre à la fin). Un article déjà classé retourne dans son rayon. Sinon, il prend
  celui que Claude propose, ou va dans « Autre ».
- Toucher un article ouvre sa fiche : nom, quantité, étiquettes et rayon. Le rayon choisi est
  retenu pour la prochaine fois.
- Un article coché reste barré jusqu'à « Vider les achetés ». Ceux qu'on ajoute souvent sont
  proposés en raccourci.
- La page se met à jour toute seule quand l'autre personne modifie la liste.
- « Toujours en stock » : les produits qu'on a toujours (achetés en vrac, par exemple). Claude
  ne les ajoute jamais, même pour une recette, sauf si on lui dit qu'il en manque. On peut
  toujours les ajouter soi-même.

## Tâches

- Un tableau à trois colonnes, À faire, En cours et Fait, affichées en onglets. Fait ne montre
  que les tâches finies dans les 14 derniers jours.
- Une tâche a un titre, une priorité (Basse, Normale, Haute), une personne (ou personne), une
  échéance et une note facultatives. Dans chaque colonne, elle se classe par priorité, puis
  par échéance. Une échéance dépassée est en rouge.
- Un filtre : Tout le monde, Moi, l'autre personne, Personne.
- « En cours → » ou « Fait → » sur la carte fait avancer la tâche. Toucher la carte ouvre sa
  fiche, où l'on peut tout changer, y compris la ramener dans une colonne précédente, ou la
  supprimer.
- Les jours affichés suivent l'heure de l'Est (`America/Toronto`).

## Gros achats

- Les dépenses à planifier, comme un système de son pour la music room. Un achat a un titre,
  des étiquettes (la pièce, le projet), une priorité, un statut (Idée, À l'étude, Prêt à
  acheter, Acheté), un budget estimé, un prix payé et des détails en texte libre.
- Les montants sont en dollars entiers : « 1 200 $ », « ~1200 » ou « 479,99 » (arrondi à
  480).
- En haut, le total des budgets par statut, « Prêt à acheter » en évidence.
- La liste se trie par priorité, puis par budget. Elle se filtre par statut (« En cours » par
  défaut, c'est-à-dire tout sauf Acheté) et par étiquette. Sous les achats en cours, ceux des
  30 derniers jours ; la puce « Acheté » les montre tous.
- Un achat fait compare le prix au budget : « Payé 480 $ · budget 550 $ · 70 $ sous le
  budget ».
- Les liens des détails sont cliquables dans la fiche.

## Serveur MCP

Sur `/mcp` (HTTP streamable, sans état), protégé par le jeton `FRIGO_MCP_TOKEN`. Sans jeton
configuré, il est désactivé. Il ne passe pas par oauth2-proxy : Claude Code le joint
directement.

| Outil | Rôle |
|---|---|
| `list_groceries` | ce qu'il reste à acheter (et les achetés sur demande), filtrable par étiquette, plus les produits toujours en stock et les rayons |
| `add_groceries` | ajoute des articles, avec une étiquette commune (ex. la recette) et un rayon par article ; même fusion que l'interface. Laisse de côté les produits toujours en stock, sauf `include_staples` |
| `update_grocery` | change le nom, la quantité, les étiquettes ou le rayon d'un article |
| `mark_bought`, `remove_groceries`, `clear_bought` | cocher, retirer, vider les achetés |
| `list_tasks` | les tâches à faire et en cours (les faites sur demande), filtrables par colonne et par personne, plus la date du jour et les prénoms possibles |
| `add_tasks` | ajoute des tâches ; si l'une est invalide, aucune n'est ajoutée |
| `update_task` | change n'importe quel champ d'une tâche, dont sa colonne |
| `remove_tasks` | supprime des tâches |
| `list_purchases` | les gros achats en cours (les achetés sur demande), filtrables par statut et par étiquette, plus les totaux et les étiquettes existantes |
| `add_purchases` | ajoute des gros achats ; si l'un est invalide, aucun n'est ajouté |
| `update_purchase` | change n'importe quel champ d'un gros achat ; un montant à 0 l'efface |
| `remove_purchases` | supprime des gros achats |

Les articles, les tâches et les gros achats ajoutés par le MCP portent une pastille « Claude ». Les consignes
données à Claude (une recette = un appel, réutiliser le nom d'un article équivalent) sont dans
`app/mcp_server.py`.

```bash
claude mcp add --transport http frigo http://<hôte>:8000/mcp --header "Authorization: Bearer $FRIGO_MCP_TOKEN"
```

## Lancer en local

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
FRIGO_DEV_USER=Jacob uvicorn app.main:create_app --factory --reload
```

Ou avec l'image :

```bash
docker build -t frigo . && docker run --rm -p 8000:8000 -e FRIGO_DEV_USER=Jacob frigo
```

## Configuration

| Variable | Rôle |
|---|---|
| `FRIGO_PEOPLE` | JSON courriel → prénom : `{"jacob@example.com": "Jacob"}`. Les prénoms sont aussi ceux à qui on peut confier une tâche |
| `FRIGO_TRUSTED_PROXIES` | IP autorisées à transmettre `X-Email`, séparées par des virgules (nginx) |
| `FRIGO_DEV_USER` | Développement seulement : saute l'identification, tout le monde est cette personne |
| `FRIGO_MCP_TOKEN` | Jeton du serveur MCP (`openssl rand -hex 32`). Vide : MCP désactivé |
| `FRIGO_DATA_DIR` | Dossier de la base SQLite (`/app/data` dans l'image) |

L'identité vient de l'en-tête `X-Email` posé par oauth2-proxy. Il n'est accepté que d'une
adresse de `FRIGO_TRUSTED_PROXIES` : sinon, n'importe qui joignant le port directement pourrait
se faire passer pour l'un de nous. Uvicorn tourne avec `--no-proxy-headers` pour que l'adresse
vérifiée soit bien celle de la connexion. Sans identité reconnue, tout répond 403, sauf
`/healthz`, `/static/` et `/mcp`, qui exige le jeton.

## Vérifications

```bash
ruff check . && ruff format --check . && pytest
```

## Publication

GitHub Actions lance le lint et les tests à chaque PR, construit l'image et la démarre pour un
test de fumée. Après un merge dans `main`, l'image est publiée sur Docker Hub sous
`lams364/frigo:latest` et `lams364/frigo:sha-<commit>`. Watchtower la récupère ensuite sur le
serveur.

La publication demande un jeton Docker Hub (Account settings > Personal access tokens,
Read & Write) dans les secrets du dépôt :

```bash
gh secret set DOCKERHUB_TOKEN -R jacoblamarche/frigo
```

## Revue Claude

À l'ouverture d'une PR (hors brouillon), Claude la relit avec le plugin `code-review` et
commente les problèmes qu'il juge sûrs. Il faut l'app GitHub
[Claude](https://github.com/apps/claude) installée sur le dépôt, et un jeton d'abonnement
Claude dans les secrets :

```bash
claude setup-token
gh secret set CLAUDE_CODE_OAUTH_TOKEN -R jacoblamarche/frigo
```

Le déploiement (LXC, nginx, oauth2-proxy) vit dans
[iac-homelab](https://github.com/jacoblamarche/iac-homelab).
