# Cinema watcher — 70mm / IMAX v Cinema City

Sleduje premietania (predvolene *Odyssea* v 70mm/IMAX sále v Cinema City Praha
Flora), počíta voľné miesta a **pošle e-mail, keď sa dá niečo kúpiť** — teda keď
pribudne nový termín alebo sa uvoľnia miesta vo vypredanom premietaní.

Tri kusy, ktoré sa dajú používať aj samostatne:

| Čo | Kde | Načo |
|---|---|---|
| `cinema_watcher.py` | lokálne / kdekoľvek | samotná kontrola, porovnanie so stavom, e-mail |
| `.github/workflows/cinema-watcher.yml` | GitHub Actions | spúšťa kontrolu podľa cronu, drží stav, posiela e-maily |
| `web/` + `netlify/` | Netlify | stránka s výsledkami + tlačidlo „Skontrolovať teraz“ |

E-maily posiela **vždy skript** (na GitHube), nie stránka. Stránka je len výklad
— zobrazuje výsledky a vie kontrolu spustiť.

---

## 1. Lokálne spustenie

Netreba nič inštalovať, stačí Python 3.8+ (žiadne závislosti mimo stdlib).

```powershell
.\run-local.ps1                            # jedna kontrola
.\run-local.ps1 --watch --interval 600     # každých 10 minút
.\run-local.ps1 --test-email               # overenie SMTP nastavenia
```

**Používaj `run-local.ps1`, nie `python cinema_watcher.py` priamo.** Wrapper
prepne stav a log do `local/` (mimo gitu). Priamy beh by písal do
`cinema_watcher_state.json`, ktorý do repa commituje GitHub Actions — a to má
dva nepríjemné následky: každý `git pull` by končil konfliktom a lokálny beh by
„zjedol“ zmeny, teda označil ich za videné, takže beh na GitHube by o nich už
nemal čo poslať mailom.

Prvý beh si len založí stav a e-mail nepošle — inak by prvý mail obsahoval
všetkých ~56 termínov. Prepínač: `--mail-first-run`.

### E-mail

Pre lokálne behy je najjednoduchšie založiť `local\.env` (je v .gitignore,
`run-local.ps1` si ho načíta sám) s riadkami `KEY=value`:

```
SMTP_HOST=smtp.gmail.com
SMTP_USER=ty@gmail.com
SMTP_PASS=heslo-aplikacie
MAIL_TO=ty@gmail.com
```

Alebo nastav premenné prostredia ručne (v PowerShelli `$env:SMTP_HOST = "..."`):

| Premenná | Príklad | Poznámka |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | bez nej sa e-maily neposielajú |
| `SMTP_PORT` | `587` | 587 = STARTTLS, 465 = SSL |
| `SMTP_SECURITY` | `starttls` | `starttls` / `ssl` / `none` |
| `SMTP_USER` | `ty@gmail.com` | |
| `SMTP_PASS` | *app password* | pri Gmaile **heslo aplikácie**, nie bežné heslo |
| `MAIL_TO` | `ty@gmail.com` | viac adries oddeľ čiarkou |
| `MAIL_FROM` | `ty@gmail.com` | default = `SMTP_USER` |
| `CINEMA_MAIL_ON` | `tickets` | `tickets` (default) / `all` / `never` |

**Gmail:** bežné heslo nefunguje, treba dvojfázové overenie a potom
*App password* (myaccount.google.com → Security → App passwords) — 16 znakov,
vlož ho ako `SMTP_PASS`.

Kedy mail príde:

* 🆕 nový termín v rozpise
* 🎟️ vypredané premietanie už nie je vypredané
* 🎟️ pribudli voľné miesta (niekto vrátil lístky) — hranicu meň cez `--alert-free N`
* ❌ zrušený termín **sám o sebe mail nespustí** (`--mail-on all` to zmení)

Miesta pre invalidný vozík sa od voľných miest odpočítavajú, takže mail nechodí
na sedadlá, ktoré sa nedajú normálne kúpiť (`CINEMA_WHEELCHAIR='{"IMAX VOLVO": 6}'`).

---

## 2. GitHub Actions

### Nasadenie

1. Vytvor repozitár na GitHube a pushni doň tento projekt (workflow musí byť aj
   na hlavnej vetve, inak sa nedá spúšťať ručne):
   ```bash
   git remote add origin https://github.com/MENO/REPO.git
   git push -u origin master
   ```
2. **Settings → Secrets and variables → Actions → Secrets** — pridaj:
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`, `MAIL_FROM`.
3. Voliteľne **Variables** (viditeľné, nie tajné) na zmenu sledovania bez úpravy
   kódu: `CINEMA_ID`, `CINEMA_ATTR`, `CINEMA_FILM_FILTER`, `CINEMA_DAYS_AHEAD`,
   `CINEMA_CAPACITY`, `CINEMA_WHEELCHAIR`, `CINEMA_MAIL_ON`, `SMTP_SECURITY`.
4. **Actions → Cinema watcher → Run workflow** so zapnutým `test_email` —
   overí, že SMTP funguje. Potom spusti normálne (bez `test_email`), čím sa
   založí stav.

### Kedy to beží

Čas sa nastavuje cronom v hlavičke `.github/workflows/cinema-watcher.yml`:

```yaml
on:
  schedule:
    - cron: "*/30 6-21 * * *"
```

**Cron je v UTC**, takže od pražského času odčítaj 2 h (v zime 1 h). Default
znamená každých 30 minút medzi 08:00 a 23:30 pražského času.

| Chcem | cron |
|---|---|
| každých 15 minút, non-stop | `*/15 * * * *` |
| každú hodinu | `0 * * * *` |
| dvakrát denne 9:00 a 17:00 (leto) | `0 7,15 * * *` |
| každých 10 minút len ráno 8–11 (leto) | `*/10 6-9 * * *` |

Dva detaily GitHubu, s ktorými sa nedá nič robiť:

* naplánované behy sa pri záťaži oneskoria o 5–20 minút, občas jeden vypadne —
  na sledovanie lístkov to stačí, ale „presne o 9:00“ negarantuje;
* ak v repe 60 dní nič nerobíš, GitHub cron **vypne** a pošle o tom mail; stačí
  ho v Actions znova zapnúť (commity od workflowu sa ako aktivita nepočítajú).

### Prečo workflow commituje do repa

Porovnanie „čo je nové“ potrebuje stav z minulého behu, a runner je po každom
behu zahodený. Workflow preto po kontrole commitne `cinema_watcher_state.json`
a `web/data/status.json` späť do repa. Push cez `GITHUB_TOKEN` nespúšťa ďalšie
workflowy, takže sa tým nedá vyrobiť smyčka.

---

## 3. Stránka na Netlify

```
web/index.html      stránka (žiadny build, žiadne závislosti)
web/config.js       odkiaľ brať dáta, ako často obnovovať
web/data/status.json  dáta z posledného behu (commituje workflow)
netlify/functions/status.mjs  GET /api/status — načíta dáta priamo z GitHubu
netlify/functions/check.mjs   POST /api/check — spustí workflow (tlačidlo)
```

Stránka zobrazuje rozpis s voľnými miestami, súčty, históriu hlásení a tlačidlo
**Skontrolovať teraz**, ktoré spustí workflow v GitHub Actions. Kontrola aj
e-mail teda vždy prebehnú na GitHube — stránka sa nesnaží nič posielať sama
(SMTP heslo by muselo byť v prehliadači, a to nechceme).

### Nasadenie

1. Netlify → **Add new site → Import an existing project** → vyber repo.
   Build command nechaj prázdny, publish directory `web` (číta sa z `netlify.toml`).
2. **Site configuration → Environment variables**:

   | Premenná | Hodnota |
   |---|---|
   | `GH_REPO` | `MENO/REPO` |
   | `GH_TOKEN` | GitHub PAT — fine-grained token na ten repo s právami *Actions: Read and write* a *Contents: Read-only* |
   | `GH_BRANCH` | `master` (ak máš inú hlavnú vetvu, uprav) |
   | `GH_WORKFLOW` | `cinema-watcher.yml` (default, netreba nastavovať) |
   | `TRIGGER_PASSWORD` | voliteľné heslo pre tlačidlo — **bez neho môže kontrolu spustiť ktokoľvek, kto pozná URL** |
3. Deploy. Hotovo.

### Dva režimy dát (a prečo to riešiť)

* **Cez `/api/status` (default).** Funkcia si stav vytiahne priamo z GitHubu,
  takže stránka vidí nové dáta hneď po behu workflowu a Netlify nemusí robiť
  nový deploy. Preto je v `netlify.toml` pravidlo `ignore`, ktoré build zruší,
  keď sa zmenili len dáta — inak by ~48 commitov denne zjedlo mesačný limit
  build minút. Funguje aj s privátnym repom.
* **Bez funkcií.** Ak nechceš PAT, zmaž v `netlify.toml` riadok `ignore` a
  v `web/config.js` nastav `statusEndpoint: ""` a `checkEndpoint: ""`. Stránka
  potom číta `data/status.json` nasadený spolu s webom a každý commit stavu
  vyvolá nový deploy. Alternatíva pri verejnom repe: nechaj `statusEndpoint`
  prázdny a do `dataUrl` daj
  `https://raw.githubusercontent.com/MENO/REPO/master/web/data/status.json`.

Ak chceš pri každom behu vynútiť deploy (druhý režim), pridaj do GitHub Secrets
`NETLIFY_BUILD_HOOK` (Netlify → Build & deploy → Build hooks) — workflow ho
zavolá po commite.

---

## Riešenie problémov

| Príznak | Kde hľadať |
|---|---|
| Mail nechodí | Actions → log behu; hľadaj `E-mail:` a `E-mail odoslaný`. Skús `Run workflow` s `test_email`. |
| „Zmeny sú, ale žiadne kúpiteľné miesta“ | Správne chovanie — zmenil sa len rozpis (zrušený termín), nič sa nedá kúpiť. |
| Filter nesadol | Log vypíše, aké názvy API ponúklo. API vracia české názvy (*Odyssea*), preto default `odys`. |
| Stránka hlási staré dáta | Beží cron? (Actions → workflow zapnutý.) Prípadne je nastavený `statusEndpoint`, ale chýba `GH_TOKEN`. |
| Tlačidlo hlási 501 | V Netlify nie sú `GH_REPO` / `GH_TOKEN`. |
| Tlačidlo hlási 401 | Nastavené `TRIGGER_PASSWORD` — stránka sa spýta na heslo, drží ho v `sessionStorage`. |
| Prázdne počty voľných miest | Neznáma kapacita sály — doplň `CINEMA_CAPACITY`, napr. `{"IMAX VOLVO": 384}`. |

Rady a čísla sedadiel verejné API nedáva (mapa sedadiel je v rezervačnom
systéme za Cloudflare), takže hlásenia vedia povedať len *koľko* miest je
voľných, nie *ktoré*.
