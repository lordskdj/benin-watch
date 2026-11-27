# Déploiement de Bénin Watch

Architecture actuelle (aucun autre service requis) :

1. **Cloudflare Worker** (`canary/`) : surveillance principale, environ toutes les minutes.
2. **GitHub Actions** (`.github/workflows/monitor.yml`) : surveillance secondaire, watcher Python complet.
3. **ntfy** pour les notifications, **healthchecks.io** pour détecter un chemin qui se tait.

Règle d'or : **aucune valeur secrète dans le dépôt**, ni dans un commit, ni dans une issue, ni dans un log. Ce document ne cite que des *noms* de secrets.

---

## 1. ntfy

1. Installer l'app ntfy sur le téléphone et s'abonner à un topic au **nom long et imprévisible** (le nom fait office de mot de passe sur ntfy.sh).
2. Optionnel : réserver/protéger le topic et créer un jeton d'accès (`tk_…`).
3. Sur iPhone, autoriser les notifications ntfy (et les notifications « urgentes »/sensibles au temps si proposé).

## 2. healthchecks.io

Créer **deux** checks distincts (un par chemin), avec une intégration (e-mail…) qui ne passe **pas** par ntfy :

| Check | Période | Grâce | Utilisé par |
|---|---|---|---|
| Canari Cloudflare | 1 min | 5 min | secret du Worker |
| GitHub Actions | 30 min | 60 min | secret GitHub `HEALTHCHECK_URL_GH` |

Les retards des horaires GitHub justifient une grâce large. Après passage du dépôt en public et du workflow à ~10 min : période 10 min, grâce 30 min.

Un ping « succès » signifie « le watcher a terminé et peut livrer ses alertes », **pas** « le site a été vérifié avec succès ». Un ping `/fail` est envoyé si une alerte ne peut pas être livrée ou en cas d'erreur fatale.

## 3. GitHub Actions (surveillance secondaire)

### Secrets (Settings → Secrets and variables → Actions)

| Nom | Obligatoire | Contenu |
|---|---|---|
| `NTFY_TOPIC` | oui | nom exact du topic (pas l'URL) |
| `NTFY_TOKEN` | non | jeton ntfy si le topic est protégé |
| `HEALTHCHECK_URL_GH` | recommandé | URL de ping du check « GitHub Actions » |

### Fonctionnement

- `monitor.yml` se lance sur `schedule` (toutes les 30 min, ~10 min une fois le dépôt public) et manuellement (`workflow_dispatch`).
- L'état (`watcher_state.json`, sa sauvegarde, `self_alert.json`) est conservé dans le **cache Actions**, jamais poussé dans Git. Le workflow n'a que `contents: read`. En cas de cache vide, la version commitée de `watcher_state.json` sert de point de départ.
- Un job **rouge** signifie : site inaccessible, ou alerte pas encore livrée (elle reste en file et repart au run suivant).
- `tests.yml` lance la suite de tests sur chaque push et pull request. Il n'a accès à aucun secret.

### Vérification après mise en place

1. Actions → *Benin Watch V2 (full watcher)* → **Run workflow** avec `test_notification: true` : une notification de test doit arriver.
2. Relancer avec `false` : vérifier dans les logs les lignes `[SITE] OK …`, puis le ping du check healthchecks « GitHub Actions ».

## 4. Canari Cloudflare (surveillance principale)

> Les fichiers `canary/` sont en préparation. Cette section sera complétée avec eux.

Principe : un Worker sans dépendance, déclenché par un Cron Trigger (`* * * * *`), sans route HTTP publique. Il demande la page avec `If-None-Match` / `If-Modified-Since`, ne suit pas les redirections, compare une empreinte SHA-256, alerte ntfy, puis envoie son ping healthchecks. L'état minimal est dans Workers KV et n'est écrit **qu'après** la livraison de l'alerte.

Prévu :

- Compte Cloudflare gratuit (aucun nom de domaine nécessaire), Node.js pour l'outil `wrangler`.
- Secrets du Worker, ajoutés avec `npx wrangler secret put <NOM>` (jamais dans `wrangler.toml`) : `NTFY_TOPIC`, `NTFY_TOKEN` (optionnel), `HEALTHCHECK_URL`.
- Limites du plan gratuit à respecter : 5 Cron Triggers par compte, 100 000 requêtes/jour, 1 000 écritures KV/jour (le canari n'écrit qu'aux changements d'état).

## 5. Avant de rendre le dépôt public

- [ ] Settings → Emails : « Keep my email addresses private » et « Block command line pushes that expose my email ». Les commits utilisent l'adresse `noreply` de GitHub.
- [ ] Settings → Actions → General → Workflow permissions : **Read repository contents**.
- [ ] Même page : décocher « Allow GitHub Actions to create and approve pull requests ».
- [ ] Même page : PR de forks → **Require approval for all external contributors**.
- [ ] Settings → Code security : activer **Secret scanning** et **Push protection**.
- [ ] Settings → Rules : règle sur `main` interdisant le force-push et la suppression.
- [ ] Les logs des anciennes exécutions Actions deviennent publics (les secrets y sont masqués par GitHub) ; les supprimer si souhaité.
- [ ] Après passage en public : filet de sécurité à ~10 min dans `monitor.yml`, et ajuster le check healthchecks GitHub.

À savoir : sur un dépôt public, GitHub **désactive les workflows `schedule` après 60 jours sans activité** du dépôt. Le check healthchecks « GitHub Actions » le signalerait ; il suffit alors de réactiver le workflow (ou de faire un commit).

---

## Option future : scheduler externe pour GitHub Actions

**Non utilisé actuellement.** L'architecture fonctionne sans.

Un service de cron externe peut appeler l'API GitHub (`workflow_dispatch`) toutes les ~5 minutes, ce qui évite les retards des horaires `schedule`. Le `schedule` resterait en place comme filet de sécurité.

Si cette option est retenue un jour :

- Créer un **fine-grained personal access token** limité à **ce seul dépôt**, avec la seule permission **Actions : Read and write**, et une date d'expiration (noter la date de renouvellement).
- Requête : `POST https://api.github.com/repos/<propriétaire>/<dépôt>/actions/workflows/monitor.yml/dispatches`, en-têtes `Authorization: Bearer <jeton>` et `Accept: application/vnd.github+json`, corps `{"ref":"main"}`.
- Un jeton qui fuit ne permet que de lancer des vérifications ; le révoquer immédiatement dans GitHub si besoin.
- Le service externe devient un point de défaillance du déclenchement : le check healthchecks « GitHub Actions » le surveille indirectement.

---

## Option future : VPS (Ubuntu 24.04 + systemd)

**Non utilisé actuellement.** Les fichiers de `deploy/` permettent de faire tourner le watcher complet toutes les 2 minutes.

| Fichier | Destination |
|---|---|
| `deploy/benin-watch.service` | `/etc/systemd/system/` |
| `deploy/benin-watch-failure.service` | `/etc/systemd/system/` (alerte si l'unité échoue) |
| `deploy/benin-watch.timer` | `/etc/systemd/system/` (toutes les 2 min) |
| `deploy/journald-benin-watch.conf` | `/etc/systemd/journald.conf.d/benin-watch.conf` |
| `deploy/benin-watch.env.example` | copie remplie dans `/etc/benin-watch/benin-watch.env` (root, mode 0600) |

Étapes :

```bash
sudo apt install python3 tzdata
sudo useradd --system --no-create-home --shell /usr/sbin/nologin benin-watch
sudo mkdir -p /opt/benin-watch /etc/benin-watch
# copier watcher.py, benin_watch/ et DEPLOY.md dans /opt/benin-watch (propriétaire root, lecture seule)
sudo install -m 0600 -o root -g root deploy/benin-watch.env.example /etc/benin-watch/benin-watch.env
sudoedit /etc/benin-watch/benin-watch.env        # renseigner NTFY_TOPIC, HEALTHCHECK_URL…
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/journald-benin-watch.conf /etc/systemd/journald.conf.d/benin-watch.conf
sudo systemctl restart systemd-journald
sudo systemd-analyze verify /etc/systemd/system/benin-watch.service
sudo systemd-analyze security benin-watch.service
sudo systemctl daemon-reload
sudo systemctl enable --now benin-watch.timer
```

Vérifier : `systemctl list-timers benin-watch.timer`, `journalctl -u benin-watch -n 50`.
L'état est dans `/var/lib/benin-watch` (créé par systemd). Arrêter les rappels :
`sudo -u benin-watch BW_STATE_DIR=/var/lib/benin-watch python3 /opt/benin-watch/watcher.py --ack`.

Codes de sortie : `0` OK, `10` dégradé (site inaccessible ou alerte pas encore livrée, déjà signalé), `75` autre instance en cours, `3` erreur fatale (auto-alerte envoyée) : seul ce dernier fait échouer l'unité volontairement.

Si un VPS est mis en place, remettre `BW_REMINDER_MINUTES: 'off'` dans `monitor.yml` pour éviter deux séries de rappels.
