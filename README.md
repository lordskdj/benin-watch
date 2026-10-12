# Bénin Watch V1.1 🇧🇯

Surveille https://voyage.benin.bj/ **à partir du HTML visible côté serveur**, sans réserver de billet. Les changements de page sont des *indices*, pas une disponibilité confirmée.

## Mise à niveau depuis V1

1. **Dans GitHub Desktop**, `Fetch origin` puis `Pull origin` si nécessaire, avant toute modification. Le workflow peut avoir créé des commits d'état.
2. Remplacer `watcher.py`, `test_watcher.py` et `.github/workflows/monitor.yml` par les fichiers V1.1. Ajouter `.gitignore`. Aucun besoin de remplacer `watcher_state.json` : **conserver l'état existant**.
3. Dans VS Code, ouvrir le dossier local `benin-watch` et éventuellement lancer `python -m unittest -v test_watcher.py`.
4. Dans GitHub Desktop, vérifier les fichiers modifiés, committer `fix: resilient monitoring V1.1`, puis `Push origin`.
5. Dans GitHub > Actions, sélectionner **Benin Watch V1.1**, lancer le workflow avec `test_notification: true`, vérifier le message sur iPhone.
6. Relancer avec `test_notification: false`, vérifier les logs `[SITE]`, `[NTFY]`, `[QUEUE]`, le dernier état `watcher_state.json` et la notification quotidienne.
7. Laisser le planning **30 minutes** pendant les tests, puis réévaluer la fréquence, les quotas GitHub, et le besoin d'un VPS.

## Résilience

- HTTP 408/425/429/5xx transitoires ou erreurs réseau : 3 tentatives pour le site, 4 pour ntfy, avec délais bornés.
- Avant chaque tentative de notification, l'alerte est **enregistrée** dans `watcher_state.json`. Elle n'est retirée qu'après la réponse HTTP 2xx de ntfy.
- Le workflow commite l'état, même si la vérification échoue (sauf annulation). Le job reste **rouge** quand le site est inaccessible ou ntfy n'a pas accepté le message.
- Alerte urgente prioritaire devant les rapports quotidiens dans la file.
- Rapport journalier après un contrôle réussi ; en cas d'indisponibilité ntfy, seul le **dernier rapport** est gardé en attente.
- Pas de commit à chaque scan identique : enregistrement lors d'un changement significatif/rapport quotidien/incident.
- Les étapes de tests sont exécutées avant chaque contrôle du site.
- Actions `checkout@v5` et `setup-python@v6` utilisent Node.js 24, compatible avec les runners récents hébergés par GitHub.

## Limites connues (à améliorer après validation)

- GitHub Actions peut retarder/omettre des exécutions : **ne garantit pas la détection instantanée**, surtout pour les billets à forte demande.
- L'analyse du texte HTML **ne garantit pas** la détection des réservations si celles-ci sont gérées par JavaScript ou une API. Des changements non pertinents peuvent créer de fausses alertes.
- Une notification acceptée par ntfy n'est pas une preuve de réception sur iOS. La file d'attente fournit une sémantique *au moins une fois*, donc quelques doublons restent possibles si GitHub échoue à pousser l'état après un envoi.
- L'absence de rapport quotidien doit être contrôlée manuellement jusqu'à l'ajout d'un **monitoring externe indépendant**.
- Si le dossier JSON est corrompu, une erreur fatale empêche le contrôle ; examiner les logs et restaurer un état valide (ne pas supprimer un état sans en discuter).
- Éviter de surcharger le site, respecter ses restrictions, ne pas contourner CAPTCHA ou files d'attente.

## Sécurité

Définir `NTFY_TOPIC` exclusivement dans les **Actions secrets** de GitHub (nom exact du topic, pas l'URL). Préférer un nom long et imprévisible. Un topic ntfy.sh non protégé est accessible à quiconque connaît son nom. Ne pas mettre de secret dans `.env` commité.

## Exécuter les tests

```bash
python -m unittest -v test_watcher.py
```

Les messages `HTTP 502` visibles dans certains tests sont **simulés** et n'indiquent pas une panne réelle.
