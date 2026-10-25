# Bitpanda Pilote

Agent d'investissement automatique pour Bitpanda, avec un budget hebdomadaire ajustable (50 € par défaut), réparti en **70 % ETF solides et 30 % actions** de grandes entreprises, classées par thème : IA, tech, luxe, pharma et « opportunités ». Aucun achat de crypto (tes cryptos existantes ne sont jamais vendues par le bot). Il tourne gratuitement sur GitHub Actions, du lundi au vendredi en milieu d'après-midi, et démarre **en simulation** : aucun euro réel n'est engagé tant que tu ne l'as pas décidé.

> ⚠️ Ce bot est un outil, pas un conseil en investissement. Il peut perdre de l'argent, comme toute stratégie. N'y mets que des sommes dont tu peux te passer.

---

## Ce qu'il fait chaque jour

1. **Il lit le marché** (sources publiques gratuites) : cours de chaque actif sur 2 ans (les actions américaines sont converties en euros), VIX (la peur sur les actions), indice Fear & Greed crypto.
2. **Il calcule ses signaux**, comme un trader systématique : tendance (moyennes 50 et 200 jours), momentum relatif sur 1-3-6 mois (« suivre les plus forts »), RSI (repli ou surchauffe), volatilité.
3. **Option : il lit l'actualité.** Si tu ajoutes une clé API Claude, une veille fait jusqu'à 5 recherches web (banques centrales, chiffres macro, résultats des entreprises suivies, événements crypto…) et nuance le montant du jour de ±20 % au maximum. Elle peut suspendre un achat un jour donné, jamais déclencher une vente.
4. **Il décide combien investir aujourd'hui** :
   - rythme de base : 1/5 du budget hebdo par jour ;
   - × un **coefficient d'opportunité** de 0 à 2,5 : marché baissier → ralentit (×0,5) ; repli dans une tendance saine ou peur excessive → accélère (jusqu'à ×1,75 par signal) ; surchauffe ou euphorie → lève le pied ;
   - le non-dépensé s'accumule (jusqu'à 4 semaines de budget) pour profiter des vraies occasions ;
   - **le vendredi**, au moins 40 % du budget de la semaine est investi sur l'ETF « socle » (MSCI World), même si rien n'est attractif : c'est la part « achats réguliers ».
5. **Il choisit quoi acheter** : dans chaque poche, les 1 ou 2 actifs les plus forts du moment, uniquement s'ils sont en tendance haussière. Pour les actions, le classement rapporte le momentum à la volatilité, pour ne pas toujours prendre les titres les plus nerveux. Si aucune action n'est en tendance haussière, l'argent prévu part sur les ETF. Ordre minimum 10 € (en dessous, il attend).
6. **Il vend seulement les lignes « satellites »** qu'il a lui-même achetées : stop suiveur (−15 % ETF, −20 % actions, −25 % crypto depuis le plus haut), cassure de tendance, ou prise de bénéfices partielle (25 % de la ligne à +30 % en surchauffe). Jamais avant 14 jours de détention. Le socle (MSCI World, Bitcoin) n'est jamais vendu.
7. **Il écrit son journal** (`journal/paper/DERNIER.md` ou `journal/live/DERNIER.md`) : ce qu'il a fait, pourquoi, l'état du portefeuille, et un registre `operations.csv` utile pour ta déclaration d'impôts.

### Garde-fous

- Mode réel verrouillé par **deux interrupteurs** (`mode: live` dans `config.yaml` **et** variable GitHub `LIVE_TRADING = true`).
- Refuse de trader en réel si aucun **plafond journalier** n'est posé sur la clé API Bitpanda.
- Contrôle chaque devis Bitpanda avant de l'accepter : prix à moins de 3 % du prix de référence, frais ≤ 2,5 %, devis non expiré.
- Ne touche **jamais** à ce que tu possédais déjà sur Bitpanda : il ne gère que les quantités qu'il a achetées.
- En cas de doute sur l'exécution d'un ordre (coupure réseau), il s'arrête pour la journée et te prévient.
- Plafonds : 1 semaine de budget par jour au maximum ; actions ≤ 40 % du portefeuille ; un thème d'actions ≤ 15 % ; une action seule ≤ 8 % ; un ETF satellite ≤ 40 %.
- Une action n'est achetée que si le consensus des analystes (Yahoo Finance) est « achat » : note moyenne ≤ 2,5 sur 5, au moins 5 analystes, objectif de cours au-dessus du cours actuel.
- La clé API Bitpanda ne peut **pas** retirer d'argent (ce droit n'existe pas dans l'API).

---

## Installation (environ 20 minutes, une seule fois, sur un ordinateur)

### 1. Créer le dépôt GitHub (privé !)

1. Va sur [github.com/new](https://github.com/new), nom : `bitpanda-pilote`, coche **Private**, puis **Create repository**.
2. Envoie les fichiers. Le plus simple en ligne de commande, depuis le dossier décompressé :
   ```bash
   git init && git add . && git commit -m "Bitpanda Pilote"
   git branch -M main
   git remote add origin https://github.com/KarlFerdinand/bitpanda-pilote.git
   git push -u origin main
   ```
   (Alternative sans terminal : **Add file → Upload files** et glisse tout le contenu du dossier. Vérifie que le dossier caché `.github` est bien envoyé : sur Mac, `Cmd + Maj + .` affiche les fichiers cachés.)

### 2. Lancer la simulation (rien d'autre à faire)

Onglet **Actions** du dépôt → active les workflows si GitHub le demande. Le bot tournera tout seul chaque jour ouvré. Pour un premier essai immédiat : **Bitpanda Pilote → Run workflow → run**.

Le journal apparaît dans `journal/paper/DERNIER.md` (lisible depuis l'appli GitHub sur téléphone).

### 3. Notifications sur ton téléphone (optionnel, gratuit)

1. Installe l'appli **ntfy** (Android / iOS).
2. Invente un nom de canal long et imprévisible, par ex. `pilote-ivan-7f3k9q2x` (quiconque connaît le nom peut lire les messages), et abonne-toi dans l'appli.
3. Dans GitHub : **Settings → Secrets and variables → Actions → New repository secret** : nom `NTFY_TOPIC`, valeur = ton nom de canal.

Tu recevras une notification à chaque achat, vente ou problème.

### 4. Veille d'actualité par Claude (optionnel)

1. Crée une clé sur [console.anthropic.com](https://console.anthropic.com) (crédit prépayé ; compte environ 1 à 3 € par mois pour 5 recherches par jour ouvré).
2. Ajoute le secret `ANTHROPIC_API_KEY`.

Sans cette clé, le bot fonctionne normalement, uniquement sur les signaux chiffrés.

### 5. Connecter Bitpanda (quand tu veux préparer le passage au réel)

1. [Bitpanda → Paramètres → Clés API](https://app.bitpanda.com/my-account/apikey) → **Nouvelle clé API** :
   - nom : `Bitpanda Pilote`
   - droits : **Trade (Read)**, **Trade (Write)**, **Balances** — rien d'autre
   - expiration : 1 an maximum (mets-toi un rappel pour la renouveler)
   - liste d'IP autorisées : laisse-la désactivée (les serveurs GitHub changent d'adresse à chaque lancement ; le plafond journalier compense)
2. Copie la clé (affichée une seule fois) dans le secret GitHub `BITPANDA_API_KEY`.
3. **Actions → Run workflow → check** : le bot vérifie la connexion, trouve chaque ETF et chaque action sur Bitpanda et compare les prix. Résultat dans `journal/VERIFICATION.md`. Un actif introuvable sera simplement ignoré ; un écart de prix ⚠️ signale un code Yahoo à corriger dans `config.yaml`.
4. **Actions → Run workflow → set-limit** : pose sur la clé un plafond d'achat de 55 € par jour (budget × 1,1). Pour une autre valeur, crée d'abord la variable `DAILY_BUY_LIMIT_EUR`.

### 6. Passer en réel

Conseil : laisse tourner la simulation 2 à 4 semaines et lis les journaux avant.

1. Alimente ton compte Bitpanda en euros (le bot n'effectue aucun virement ; un virement permanent hebdomadaire du montant de ton budget est le plus simple).
2. Dans `config.yaml`, remplace `mode: paper` par `mode: live`.
3. **Settings → Secrets and variables → Actions → onglet Variables → New repository variable** : `LIVE_TRADING` = `true`.

Le journal réel est dans `journal/live/`, séparé de la simulation.

---

## Au quotidien

| Je veux… | Je fais… |
|---|---|
| Analyser tout mon portefeuille Bitpanda | **Actions → Run workflow → analyse** (lecture seule). Résultat dans `journal/PORTEFEUILLE.md` : répartition, concentration, plus/moins-values, écart à la cible du bot. |
| Changer mon budget | Variable GitHub `WEEKLY_BUDGET_EUR` (ex. `30`), ou `budget.weekly_eur` dans `config.yaml`. Pense à ajuster le plafond Bitpanda si tu l'augmentes. |
| Mettre en pause | Variable `LIVE_TRADING` = `false` (retour en simulation), ou `enabled: false` dans `config.yaml` (plus rien du tout). |
| Arrêt d'urgence total | Supprime la clé API dans Bitpanda. |
| Voir ce qu'il a fait | `journal/live/DERNIER.md` (ou `paper`), ou les notifications ntfy. |
| Ajouter / retirer une action | Section `universe` de `config.yaml` : une ligne avec ISIN, code Yahoo, `currency: USD` si elle est cotée en dollars, `sleeve: actions`. Relance ensuite **check**. |
| Tenir compte (ou non) de mes autres avoirs Bitpanda | `allocation.scope` : `account` (par défaut) répartit les achats en comptant tout ton compte, sans jamais rien vendre ; `bot` ne regarde que ce que le bot a acheté. |
| Changer la répartition | `allocation.etf`, `allocation.actions`, `allocation.crypto` (total = 1). Ex. `0.8 / 0 / 0.2` pour revenir sans actions. |

Les modifications de `config.yaml` se font directement sur GitHub (icône crayon ✏️), y compris depuis un navigateur sur téléphone.

---

## À savoir avant d'engager de l'argent réel

- **Aucune garantie.** Les signaux réduisent certains risques (acheter en pleine chute, garder une ligne qui s'effondre) mais ne prédisent rien. Une stratégie qui a bien marché par le passé peut mal marcher demain.
- **Les frais comptent.** Sur Bitpanda, une transaction crypto coûte environ 1 à 2,5 % ; les ETF et actions sont moins chers mais pas gratuits (écart de prix, et change euro/dollar pour les actions américaines). C'est pour ça que le bot achète peu souvent, par tickets d'au moins 10 €, et ne revend pas avant 14 jours.
- **Fiscalité en France** (je ne suis pas conseiller fiscal, vérifie sur impots.gouv.fr) :
  - Bitpanda est un compte ouvert à l'étranger (Autriche) : il se déclare chaque année avec ta déclaration de revenus (formulaire 3916 / 3916-bis pour la partie crypto).
  - Chaque vente avec plus-value est imposable (formulaire 2086 pour les cryptos, déclaration des plus-values mobilières pour les ETF et actions). Le fichier `operations.csv` liste toutes les opérations du bot.
- **Documents d'information clés (KID).** Pour chaque ETF acheté en réel, le lien vers son KID officiel figure dans le journal : lis-les.

### Dépannage

- **« historique de cours indisponible »** pour tous les actifs : Yahoo Finance a limité les requêtes ce jour-là. Par sécurité, le bot n'achète rien sans cours de référence et réessaie le lendemain.
- **Un actif « introuvable sur Bitpanda »** : il n'est pas proposé (ou plus) sur Bitpanda ; remplace-le dans `config.yaml` ou laisse-le, il sera ignoré.
- **Un mail de GitHub « Run failed »** : ouvre le journal du jour, la cause est écrite en haut.

---

## Pour les curieux

```
config.yaml              réglages (budget, allocation, univers, garde-fous)
bot/data.py              sources de marché (Yahoo Finance, VIX, Fear & Greed)
bot/indicators.py        tendance, RSI, momentum, volatilité
bot/news.py              veille d'actualité par Claude (optionnelle)
bot/strategy.py          coefficient d'opportunité, choix des actifs, ventes
bot/budget.py            budget hebdomadaire et report du non-dépensé
bot/broker.py            ordres Bitpanda (devis contrôlé puis accepté) et simulation
bot/report.py            journal Markdown et registre CSV
bot/portfolio.py         analyse de tout le portefeuille Bitpanda (lecture seule)
bot/main.py              orchestration + commandes check / set-limit / analyse
.github/workflows/bot.yml  planification GitHub Actions
tests/                   60 tests (relancés avant chaque exécution)
```

En local : `pip install -r requirements.txt`, puis `python -m pytest` et `python -m bot.main` (simulation).

API utilisée : [API publique Bitpanda](https://docs.public.bitpanda.com) — devis (`/v1/quotes`), acceptation, portefeuille, actifs, plafonds journaliers.
