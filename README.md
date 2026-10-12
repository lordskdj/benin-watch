# Bénin Watch V1 🇧🇯

Surveille **https://voyage.benin.bj/** par analyse de texte HTML côté serveur. **Aucune promesse de détection instantanée ni de places confirmées.**

## Mise en place guidée

1. Créer un dépôt GitHub **privé** et ajouter `watcher.py` et `.github/workflows/monitor.yml` (conserver le chemin exact).
2. Aller dans **Settings → Secrets and variables → Actions → New repository secret**. Ajouter `NTFY_TOPIC` avec le nom exact du sujet ntfy existant (pas son URL). Ne jamais afficher le sujet publiquement.
3. Vérifier que les **Actions sont autorisées** et que le jeton `GITHUB_TOKEN` dispose de **Read and write permissions** dans Settings → Actions → General → Workflow permissions. Un réglage d'organisation peut primer.
4. Dans **Actions → Benin Watch V1 → Run workflow**, cocher `test_notification` puis lancer. Vérifier la notification ntfy sur l'iPhone.
5. Lancer de nouveau **sans** cocher `test_notification`. Vérifier les logs, le fichier `watcher_state.json` commité et la notification de rapport quotidien.
6. Vérifier que les déclenchements automatiques apparaissent à `:07` et `:37` UTC (horaires approximatifs et non garantis).

## Comportement

- Premier contrôle réussi : enregistre le contenu et envoie un rapport quotidien.
- Chaque jour (fuseau Europe/Paris), **premier contrôle réussi** : un rapport ntfy. **L'absence de rapport signifie qu'il faut vérifier le bot** ; elle ne déclenche pas d'alerte autonome.
- Changement d'état, texte modifié, indices de réservation : notification prioritaire, sans affirmer qu'une place est disponible.
- Erreur réseau/HTTP : une alerte par incident ; le workflow sort en échec. Au prochain contrôle réussi, notification de reprise.
- État commité uniquement après une exécution réussie. Un incident peut nécessiter une inspection des journaux et, si l'état n'a pas été persisté, des alertes répétées peuvent arriver.

## Limites importantes

- **HTML seulement** : une interface JavaScript peut échapper au contrôle. Une V2 Playwright sera utile après inspection du site.
- Le planning GitHub Actions peut être retardé, abandonné ou stoppé (quota/règles GitHub). **Pas adapté comme unique dispositif si les places partent en quelques minutes**.
- Environ 1 440 exécutions/mois au rythme choisi ; les minutes facturées peuvent dépasser les quotas selon la durée des jobs et le plan GitHub. Surveiller Billing → Actions.
- Un sujet public ntfy est connaissable par son nom ; utiliser un nom long et imprévisible. Ne pas transmettre de secrets ni de données personnelles dans les alertes.
- Aucun contournement de CAPTCHA, file d'attente ou restrictions du site ; trafic modéré.
- Le succès d'un appel ntfy garantit seulement que ntfy a accepté le message, **pas** que l'iPhone l'a affiché.

## Test local sans secret dans le dépôt

Sous PowerShell :

```powershell
$env:NTFY_TOPIC="ton-topic-prive"
python watcher.py --test
python watcher.py
```

Ne commite jamais `watcher_state.json` avec des informations sensibles ; ce fichier contient uniquement l'état de la page et les dates.
