# Bénin Watch 🇧🇯

Surveille la page **https://www.voyage.benin.bj/** (plateforme des vols spéciaux vers le Bénin) et envoie une notification sur téléphone dès qu'elle change, pour pouvoir réserver **soi-même** le plus vite possible.

> **Projet personnel**, non affilié au gouvernement de la République du Bénin, à Bénin Tours S.A. ni à Bénin Tourisme.
> Bénin Watch **ne réserve rien** : il ne remplit aucun formulaire, ne contourne aucun CAPTCHA ni file d'attente, et ne fait qu'une lecture légère de la page publique.
> Un changement de page est un **indice**, pas une disponibilité confirmée : vérifier toujours sur le site officiel.

## Architecture

```text
   Cloudflare Worker (canary/)          GitHub Actions (watcher.py)
   surveillance PRINCIPALE, ~1 min      surveillance SECONDAIRE, filet de sécurité
   très simple : la page a-t-elle       complète : classement, file d'attente,
   changé ? redirection ? panne ?       rappels, rapport quotidien
              │                                    │
              └──────────────┬─────────────────────┘
                             ▼
                ntfy (notification iPhone)
                healthchecks.io (alerte si un des deux chemins se tait)
```

- **Deux chemins indépendants** : hébergeur, langage et déclencheur différents. Une panne de GitHub n'arrête pas le canari, et inversement.
- **Le canari Cloudflare** (`canary/`) vérifie la page environ toutes les minutes avec des requêtes conditionnelles (`ETag` / `If-None-Match`). Il est volontairement minimal : il ne classe rien, il alerte sur tout changement, toute redirection, tout statut HTTP inattendu ou toute panne prolongée. Son état tient dans un Durable Object (cohérence forte : pas de double alerte).
- **Le watcher Python** (`watcher.py` + `benin_watch/`) tourne sur GitHub Actions. Il classe la page, garde une file de notifications persistante (rien n'est retiré avant que ntfy l'ait acceptée), envoie des rappels et un rapport quotidien.
- **ntfy** livre les notifications. Le titre indique la source : `BENIN WATCH CF` pour le canari Cloudflare, `BENIN WATCH GH` pour GitHub. Un même changement peut donc être notifié deux fois (une par chemin) : c'est voulu.
- **healthchecks.io** sert de « dead-man switch » : chaque chemin envoie un signal à chaque exécution ; si les signaux s'arrêtent, healthchecks prévient par son propre canal (e-mail…). Bénin Watch ne peut donc pas mourir en silence.

Fréquence actuelle de GitHub Actions : toutes les **10 minutes** (filet de sécurité). Les horaires `schedule` de GitHub sont « au mieux » : des retards ou des exécutions sautées sont possibles. C'est pourquoi la détection rapide repose sur le canari.

## Alertes

Le titre commence par `BENIN WATCH CF` (canari Cloudflare) ou `BENIN WATCH GH` (watcher Python sur GitHub).

### Canari Cloudflare (`BENIN WATCH CF`)

| Alerte | Signification | Priorité |
|---|---|---|
| `canari demarre` | Première observation réussie : prouve que toute la chaîne fonctionne (urgente si l'état initial n'est pas une page normale) | normale |
| `PAGE MODIFIEE` | Le HTML de la page a changé, même de façon invisible à l'écran | urgente |
| `REDIRECTION DETECTEE` | Le site redirige : **hôte de destination affiché**, avec « meme hote » ou « AUTRE HOTE ». La redirection n'est jamais suivie | urgente |
| `ANOMALIE HTTP` | Statut HTTP inattendu et non temporaire (403, 404…) | urgente |
| `PAGE TROP VOLUMINEUSE` | Réponse de plus de 2 Mo, non analysée ; une seule alerte par épisode | urgente |
| `PAGE STABILISEE` | Anti-spam : des changements n'ont pas été notifiés (plus de 3 alertes en 30 min) ; dès que le quota le permet, le **dernier état observé** est notifié, même si la page ne bouge plus | urgente |
| `SITE INJOIGNABLE` | Aucune réponse (réseau, délai, 5xx…) depuis 3 min ; une seule alerte par panne | haute |
| `SITE RETABLI` | Le site répond de nouveau après une panne signalée | normale |

Plusieurs événements d'une même minute sont regroupés dans une seule notification (avec la priorité la plus élevée).

### Watcher Python sur GitHub (`BENIN WATCH GH`)

| Alerte | Signification | Priorité |
|---|---|---|
| `VERIFIER LE SITE` | Mots de réservation détectés (« réserver maintenant », « rechercher un vol »…) | urgente |
| `REDIRECTION DETECTEE` | Le site redirige ailleurs. Le **domaine de destination est affiché** : vérifier qu'il est légitime avant de payer quoi que ce soit | urgente |
| `PAGE D ATTENTE DISPARUE` | La page d'attente a été remplacée par un contenu inconnu | urgente |
| `ANOMALIE SITE` | Réponse HTTP ou contenu inattendu (migration ou ouverture possible) | urgente |
| `CHANGEMENT DETECTE` | Le code de la page a changé mais le message d'attente est toujours là | haute |
| `erreur site` / `retabli` | Le site ne répond plus malgré plusieurs tentatives / répond de nouveau | haute / normale |
| `RAPPEL n/4` | Rappels 5, 15, 30 et 60 min après une alerte urgente probable | urgente |
| `rapport quotidien` | Preuve de vie quotidienne (heure de Paris) | normale |
| `WATCHER ERROR` | Le watcher lui-même a planté | urgente |

Sur GitHub, les rappels partent à l'exécution suivante (leur horaire suit donc celui du workflow) et s'arrêtent seuls après le 4ᵉ. La commande `python watcher.py --ack` (arrêt manuel des rappels) ne sert que sur une machine qui conserve l'état localement.

⚠️ **Arnaques** : de faux sites imitant l'adresse de réservation ont déjà circulé. Bénin Watch ne met dans ses notifications que l'URL officielle, ou le domaine de redirection affiché en clair.

## Limites connues

- **Aucune garantie de détection instantanée.** Le canari vérifie environ toutes les minutes ; GitHub Actions peut être retardé.
- **Lecture du HTML uniquement.** La page actuelle est statique, mais si l'ouverture se faisait uniquement par JavaScript ou par une API, seul le changement de la page serait vu, pas son contenu dynamique.
- **Ouverture ailleurs.** Si les réservations ouvraient sur un autre domaine sans que cette page change ni redirige, Bénin Watch ne le verrait pas.
- **Fausses alertes possibles.** Toute modification de la page (même cosmétique) déclenche une alerte : c'est voulu, mieux vaut une alerte de trop qu'une ouverture ratée.
- **Livraison « au moins une fois ».** Quelques doublons sont possibles ; une notification acceptée par ntfy n'est pas une preuve de réception sur le téléphone.
- **Un topic ntfy non protégé est lisible par quiconque connaît son nom** : utiliser un nom long et imprévisible, ou un jeton d'accès.

## Configuration

Aucun secret n'est dans le dépôt. Les valeurs sont stockées dans les **secrets GitHub Actions** (et, pour le canari, dans les secrets Cloudflare). Voir [DEPLOY.md](DEPLOY.md).

| Variable | Rôle |
|---|---|
| `NTFY_TOPIC` | Nom exact du topic ntfy (secret) |
| `NTFY_TOKEN` | Jeton ntfy si le topic est protégé (secret, optionnel) |
| `HEALTHCHECK_URL` | URL de ping healthchecks.io (secret ; sur GitHub : secret `HEALTHCHECK_URL_GH`) |
| `BW_REMINDER_MINUTES` | Rappels après une alerte (défaut `5,15,30,60` ; `off` pour désactiver) |
| `BW_INSTANCE` | Préfixe ajouté au titre des notifications (`GH` sur GitHub) |
| `BW_STATE_DIR` | Dossier de l'état (défaut : à côté du script) |

Toute valeur secrète est masquée dans les logs (`benin_watch/log.py`).

## Tests

```bash
python -m unittest discover -v -p "test_*.py"
```

Canari : `cd canary && node --test worker.test.js` (Node.js ≥ 20, aucune dépendance).

Aucune dépendance externe sous Linux (Python 3.12). Sous Windows, installer d'abord `tzdata` : `python -m pip install --require-hashes -r requirements.txt`.
Les messages `HTTP 502` ou `403` visibles pendant les tests sont **simulés**. Un test est ignoré sous Windows (permissions POSIX).

## Future improvements

- **Instagram (signal d'anticipation uniquement).** Les comptes officiels annoncent parfois les campagnes avant le site. Une surveillance via l'API officielle de Meta (Business Discovery) pourra être étudiée **après** stabilisation du cœur. Elle n'annoncerait jamais une ouverture : seul le site officiel la confirme.
- **Déclenchement externe de GitHub Actions** (par exemple un cron externe appelant `workflow_dispatch`) pour réduire les retards des horaires GitHub.
- **VPS** (option) : le dossier `deploy/` contient des unités systemd durcies, prêtes à l'emploi, pour faire tourner le watcher complet toutes les 2 minutes sur un serveur Ubuntu 24.04. Non utilisé actuellement.
