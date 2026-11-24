# CLAUDE.md

frigo est une petite application web pour deux personnes qui partagent une maison. Elle gère
la liste d'épicerie, les recettes, les tâches et les gros achats à planifier. Un serveur MCP
intégré à l'application permet à Claude de les remplir. Le projet est en Python 3.13 avec
FastAPI, des pages Jinja, un peu de JavaScript sans framework et SQLite. Il est conçu pour le
téléphone : 480 px de large au maximum.

## Commandes

```bash
python3.13 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest      # ce que la CI exige
FRIGO_DEV_USER=Alex uvicorn app.main:create_app --factory --host 127.0.0.1 --reload
```

`FRIGO_DEV_USER` saute l'identification, en développement seulement : uvicorn reste donc sur
`127.0.0.1`, pour que personne d'autre sur le réseau n'y accède.

## Carte du code

- `app/main.py` : `create_app()` définit toutes les routes, le middleware d'identité et le
  rendu des pages. La validation et le SQL vivent dans le module du domaine, pas dans les
  routes.
- `app/<domaine>.py` (`groceries`, `recipes`, `tasks`…) : validation, requêtes SQL et tri. Un
  module par onglet.
- `app/db.py` : connexion, `MIGRATIONS`, `now()`, `bump_version()`.
- `app/mcp_server.py` : les outils MCP, leurs modèles Pydantic et `INSTRUCTIONS`, les
  consignes envoyées à Claude.
- `app/templates/` : une page par écran. Les fragments `_*.html` sont rechargés par le
  rafraîchissement automatique (`/<onglet>/liste`). Les fiches d'édition sont des pages.
- `app/static/app.js` : les formulaires `data-async`, `data-confirm` et `data-toggle`, et le
  rafraîchissement toutes les 5 secondes par ETag.
- `tests/` : un fichier par module (`test_<module>.py`), plus `test_routes.py` et
  `test_mcp.py`.

## Règles

### Données

- `MIGRATIONS` ne fait que grandir. Ne jamais modifier une migration déjà publiée : en ajouter
  une nouvelle à la fin de la liste.
- Toute écriture se fait dans un `with con:` et appelle `bump_version(con)` quand elle change
  quelque chose. Sans ça, l'autre personne ne voit pas le changement.
- Un ajout de plusieurs éléments (par le MCP surtout) est tout ou rien : une seule
  transaction, et une erreur n'ajoute rien.
- Les valeurs passent toujours par des paramètres `?`. Une f-string SQL ne peut contenir que
  des fragments écrits dans le code.
- Les instants s'enregistrent en UTC avec `db.now()`. Le jour local vient de `tasks.today()`,
  à l'heure de l'Est. Ne jamais utiliser `date.today()` ni `datetime.now()` sans fuseau :
  l'image tourne en UTC.
- Les noms se comparent avec `groceries.normalize()` (casse, accents, espaces) et
  s'enregistrent avec `groceries.display()`.

### Sécurité

- L'identité vient uniquement de `X-Email`, et seulement depuis une IP de
  `FRIGO_TRUSTED_PROXIES`. Ne jamais lire l'identité ailleurs ni affaiblir cette vérification.
- Tout POST venu des pages passe la vérification d'origine (`same_origin`) du middleware.
- `/mcp` exige le jeton, comparé avec `hmac.compare_digest`. Sans jeton configuré, le MCP est
  désactivé. Ne jamais journaliser un jeton ni un en-tête `Authorization`.
- Jinja échappe tout : ne jamais appliquer `|safe` ni `Markup` à une donnée enregistrée.
- Un lien affiché en `href` doit être en `http` ou `https` (voir `recipes.check_url`).

### Interface

- Les URL de `/static/` passent par `static()`, qui ajoute l'empreinte du fichier : Cloudflare
  et les navigateurs gardent l'ancienne version sinon.
- Une page dont l'affichage dépend du jour inclut la date dans son ETag.
- Une fiche invalide se réaffiche avec le message et un code 4xx, sans perdre ce qui a été
  tapé.
- L'image n'a pas de locale : les jours, les mois et les montants en français sont écrits à
  la main.

### MCP

- Les noms d'outils, de champs et les valeurs (`todo`, `high`…) sont en anglais. Les
  docstrings et les descriptions `Field` sont en français : Claude les lit.
- Les erreurs de validation (`ValueError`, `LookupError`) deviennent des `ToolError`.
- Ajouter, retirer ou changer un outil demande de mettre à jour, dans la même PR,
  `INSTRUCTIONS`, le tableau du README et `test_tools_are_listed`.
- Ce que Claude ajoute porte `added_by = AUTHOR` (« Claude »). C'est une pastille dans
  l'interface, jamais du texte ajouté au nom.

### Langue et style

- En français : l'interface, les messages d'erreur, les commentaires, les docstrings, le
  README, les chemins d'URL et les champs de formulaire (`/taches`, `etiquette`). Guillemets
  « », espaces insécables dans les montants (« 1 200 $ »).
- En anglais : les identifiants Python et SQL, et les messages de commit.
- Les commentaires sont courts et expliquent seulement un « pourquoi » qu'on ne voit pas en
  lisant le code (piège, contrainte, valeur imposée). Pas de récit, pas de paraphrase du
  code, pas de commentaire de section.
- Pas de nouvelle dépendance sans nécessité. Si une dépendance est ajoutée, l'écrire dans
  `requirements.txt` avec des bornes de version.

### Tests

- Tout changement de comportement vient avec ses tests dans la même PR. Le module se teste
  avec la fixture `con`, les routes avec `client` (assertions sur le HTML), le MCP avec
  `call()` et `tool_error()`.
- Des fonctions `test_*` simples, dont le nom décrit le comportement en anglais
  (`test_one_bad_task_adds_none`). `pytest.mark.parametrize` pour les tableaux de cas.
- Un test ne dépend pas de la date du jour : il calcule ses dates à partir de
  `tasks.today()`.
- Ne pas affaiblir un test pour le faire passer.

### Documentation

- Un changement visible pour l'utilisateur met le README à jour dans la même PR.

## Façon de travailler

- Une nouvelle fonctionnalité commence par un plan, avant toute ligne de code : données,
  outils MCP, interface, cas limites, avec une recommandation pour chaque décision. On attend
  la validation. Les décisions retenues sont ensuite inscrites dans l'issue.
- Une branche par issue. Message de commit en anglais : `domaine: résumé` (ex. `taches: a
  three-column board shared by both of us`), un corps qui explique, puis `Closes #N`.
- Corps de PR en français : « Ferme #N », puis `## Ce qui change` et `## Vérifications`. Les
  vérifications ne listent que ce qui a vraiment été lancé. Dire aussi ce qui n'a pas été
  vérifié.
- Un changement d'interface se vérifie dans un navigateur à la taille d'un téléphone.
- Ne jamais committer `data/`, une base SQLite, un jeton ni une adresse courriel réelle.

## Revue de code

Ces consignes s'adressent à la revue automatique des PR.

- Écrire les commentaires en français. Commencer chaque commentaire par une phrase qui résume
  la correction proposée.
- Lire l'issue liée (« Ferme #N ») : ses décisions sont voulues. Vérifier que la PR les
  respecte, sans les remettre en question.
- Signaler toute infraction aux règles ci-dessus, en citant la règle : migration publiée
  modifiée, écriture sans `bump_version`, date sans fuseau, `|safe` sur une donnée,
  identité ou jeton affaiblis, outil MCP changé sans `INSTRUCTIONS`, README ni test,
  comportement changé sans test, commentaire qui raconte le code.
- Ne pas commenter le formatage ni le lint (ruff tourne dans la CI), ni le choix des mots de
  l'interface, sauf s'il y a une faute ou une incohérence avec le reste de l'app.
- Ne pas signaler comme problèmes : `Form()` et `Depends()` en valeur par défaut (l'usage de
  FastAPI, configuré dans ruff), les routes définies dans `create_app()`, la syntaxe de Python
  3.13.
