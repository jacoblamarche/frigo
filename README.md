# frigo

La porte du frigo, version web : la liste d'épicerie, les tâches de la maison et les gros
achats à planifier, pour deux personnes. Claude peut y ajouter des choses par un serveur MCP
intégré.

Pour l'instant : la liste d'épicerie et son serveur MCP. Les tâches et les gros achats suivent.

## Épicerie

- Ajout en texte libre : « lait 2 L », « 3 oignons », « 1 lb bœuf haché #lasagne ». La
  quantité et les étiquettes (`#recette`) sont reconnues ; `_` devient une espace dans une
  étiquette.
- « Coller une liste » ajoute une ligne par article, avec une étiquette commune facultative.
- Un article déjà à acheter n'est jamais dupliqué : la quantité s'ajoute (« 2 + 1 ») et les
  étiquettes se cumulent.
- Un article coché reste barré jusqu'à « Vider les achetés ». Ceux qu'on ajoute souvent sont
  proposés en raccourci.
- La page se met à jour toute seule quand l'autre personne modifie la liste.

## Serveur MCP

Sur `/mcp` (HTTP streamable, sans état), protégé par le jeton `FRIGO_MCP_TOKEN`. Sans jeton
configuré, il est désactivé. Il ne passe pas par oauth2-proxy : Claude Code le joint
directement.

| Outil | Rôle |
|---|---|
| `list_groceries` | ce qu'il reste à acheter (et les achetés sur demande), filtrable par étiquette |
| `add_groceries` | ajoute des articles, avec une étiquette commune (ex. la recette) ; même fusion que l'interface |
| `update_grocery` | change le nom, la quantité ou les étiquettes d'un article |
| `mark_bought`, `remove_groceries`, `clear_bought` | cocher, retirer, vider les achetés |

Les articles ajoutés par le MCP portent une pastille « Claude » dans la liste. Les consignes
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
| `FRIGO_PEOPLE` | JSON courriel → prénom : `{"jacob@example.com": "Jacob"}` |
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

Le déploiement (LXC, nginx, oauth2-proxy) vit dans
[iac-homelab](https://github.com/jacoblamarche/iac-homelab).
