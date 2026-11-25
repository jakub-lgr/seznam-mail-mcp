# Architektura

## Přehled

```
 Claude.ai / ChatGPT / jiný MCP klient
        │  HTTPS (Streamable HTTP + OAuth 2.1 s PKCE)
        ▼
 reverse proxy (nginx: TLS, rate limit, vypnuté bufferování)
        │
        ▼
 seznam-mail-mcp  (Python, systemd, 127.0.0.1:<port>, vlastní systémový uživatel)
   ├─ OAuth server    /.well-known/*, /register, /authorize, /token
   ├─ webové stránky  přihlášení, „Můj účet“ (odpojit a smazat data)
   ├─ MCP endpoint    /mcp → nástroje
   └─ IMAP/SMTP       imap.seznam.cz:993 / smtp.seznam.cz:465
        │
        ▼
 SQLite  (uživatelé, šifrovaná hesla pro aplikace, OAuth klienti, hashe tokenů)
```

Služba je zároveň **OAuth autorizační server** i **resource server** (MCP endpoint).
Nespoléhá na žádnou externí identitu.

## Přihlašovací tok

1. Uživatel vloží do klienta URL `https://<host>/mcp`.
2. Klient zjistí metadata (`/.well-known/oauth-protected-resource`,
   `/.well-known/oauth-authorization-server`), zaregistruje se přes `/register`
   (Dynamic Client Registration) a otevře `/authorize` s PKCE (S256).
3. Uživatel zadá **Seznam e-mail a heslo pro aplikaci**.
4. Server se těmito údaji hned přihlásí do IMAPu. Úspěšné přihlášení je důkaz
   identity, žádný další účet ani heslo nejsou potřeba.
5. **Allowlist:** přihlásit se mohou jen adresy, které povolil správce.
6. Heslo se zašifruje a uloží. Klient dostane access token (1 h) a refresh token
   (180 dní, rotuje se při každém použití, takže limit platí jen pro nečinnost).
7. Každé volání nástroje: token → uživatel → dešifrované údaje → jeho schránka.

## Bezpečnost

- Hesla pro aplikace jsou šifrovaná AES-GCM. Klíč je **mimo databázi**
  (systemd credential / soubor s právy 600).
- Tokeny a autorizační kódy jsou v DB jen jako SHA-256 hashe.
- Redirect URI jsou omezené na nakonfigurovaný seznam (callbacky známých klientů).
- Rate limit a dočasné zablokování po opakovaných neúspěšných přihlášeních.
- Logy neobsahují obsah e-mailů ani hesla.
- Obsah e-mailů se vrací modelu jako explicitně nedůvěryhodná data
  (ochrana proti prompt injection).
- E-maily se čtou přes `BODY.PEEK`, takže se neoznačují jako přečtené.
- Nic se nemaže trvale: „smazání“ je přesun do Koše a z Koše server mazat odmítne.
  Výjimka: koncept po odeslání přes `send_draft` (jeho kopie je v Odeslaných).
- Odesílání: max. 20 adresátů, max. `SEND_DAILY_LIMIT` (50) e-mailů za 24 h na uživatele
  (v paměti procesu), adresy se kontrolují před připojením k SMTP. Pokyny serveru
  říkají modelu, že smí odeslat jen na výslovný pokyn uživatele, nikdy kvůli obsahu e-mailu.
  Zápisové nástroje mají anotace `destructive_hint`, aby klienti chtěli potvrzení.
- Zápisové nástroje jdou vypnout (`ENABLE_ORGANIZE`, `ENABLE_SEND`).
- Přílohy se zpracovávají v procesu jako nedůvěryhodná data: max. 25 MB, 50 stran PDF,
  1000 řádků na list, kontrola velikosti po rozbalení (.docx/.xlsx), limit pixelů obrázků.
- Uživatel si může na stránce „Můj účet“ všechno smazat. Úplné odříznutí jde
  zrušením hesla pro aplikaci na Seznamu.

## Nástroje

| Tool | Popis |
|---|---|
| `list_folders` | složky a počty nepřečtených |
| `search_emails` | hledání (odesílatel, předmět, text, datum, složka, nepřečtené) → hlavičky a ID |
| `read_email` | text e-mailu (HTML → text, zkrácený), seznam příloh |
| `read_thread` | celé vlákno |
| `read_attachment` | obsah přílohy podle indexu z `read_email` (viz níže) |
| `create_draft` | uloží koncept (i odpověď) do Konceptů, neodesílá |
| `mark_emails` | `\Seen` / `\Flagged` pro až 100 zpráv |
| `move_emails` | UID MOVE do složky nebo role (inbox, archive, spam, trash), vrací nová UID z COPYUID |
| `trash_emails` | přesun do Koše |
| `send_email` | SMTP (smtp.seznam.cz:465, stejné heslo pro aplikace), kopie do Odeslaných, u odpovědi `\Answered` |
| `send_draft` | odešle koncept beze změn (Bcc jen v obálce), pak ho z Konceptů odstraní |

### Přílohy

| Typ | Výstup |
|---|---|
| PDF | text po stranách (pypdf); strany bez textové vrstvy (skeny) se vykreslí jako obrázek (pypdfium2, max. 3) |
| Word .docx | text odstavců a tabulek (vlastní parser XML) |
| Excel .xlsx | listy jako řádky `a \| b \| c` (openpyxl) |
| text, CSV, JSON, XML, HTML | text (UTF-8, záloha cp1250) |
| obrázky | zmenšené na max. 1568 px (Pillow) |
| přiložený e-mail | hlavičky a text |

Typ se bere z `Content-Type`, u `application/octet-stream` z přípony souboru.

## Stack

- Python 3.11+, oficiální `mcp` SDK v2 (`MCPServer`, Streamable HTTP, podpora OAuth)
- Starlette (webové stránky), `IMAPClient` v thread poolu (nové spojení na každé volání nástroje)

### Kód

- `mail.py`: IMAP operace, parsování e-mailů. Neví nic o MCP ani o uživatelích.
- `server.py`: MCP nástroje. Údaje ke schránce získá přes *resolver*: ve stdio režimu
  z proměnných prostředí, v HTTP režimu podle OAuth tokenu.
- `db.py`, `config.py`: databáze (PostgreSQL / SQLite) a konfigurace.
- `cryptography` (AES-GCM)
- SQLAlchemy Core (async): **PostgreSQL** (`asyncpg`) nebo **SQLite** (`aiosqlite`)

### Databáze: PostgreSQL nebo SQLite

Backend se volí proměnnou `DATABASE_URL`:

| `DATABASE_URL` | Backend |
|---|---|
| prázdná / nenastavená | SQLite soubor `DATA_DIR/seznam-mail-mcp.db`, složka i soubor se vytvoří samy |
| `postgresql://user:heslo@host:5432/db` | PostgreSQL |
| `sqlite:///cesta/k/souboru.db` | SQLite na zadané cestě |

- Jediným zdrojem pravdy o schématu jsou tabulky v `src/seznam_mail_mcp/db.py`.
- Z nich se generují `schema/postgresql.sql` a `schema/sqlite.sql`
  (`python -m seznam_mail_mcp.db dump-schema --write`). Databázi jde podle nich
  vytvořit ručně a test hlídá, že jsou aktuální.
- Server při startu chybějící tabulky vytvoří sám (`python -m seznam_mail_mcp.db init`
  udělá totéž ručně).
- PostgreSQL se hodí, když se chceš do dat dívat přes webový správce databáze.
  SQLite se hodí pro rychlé nasazení bez databázového serveru.
- Až se schéma bude měnit, přibudou migrace (Alembic, podporuje oba backendy).

## Zvláštnosti Seznam IMAP (ověřeno na skutečné schránce)

- Funguje stejně pro `@seznam.cz` i `@email.cz`. Přihlašovací jméno je celá adresa.
- Složky mají anglické názvy (`INBOX`, `sent`, `drafts`, `trash`, `spam`, `archive`,
  `newsletters`). Kromě `spam` existuje i prázdná složka `Junk`.
- `SEARCH FROM/TO/SUBJECT` hledá v indexu **celých slov bez diakritiky**: `Pozvanka`
  najde „Pozvánka“, `Pozvánka` nenajde nic a `Pozv` také nic. Server proto u těchto
  kritérií diakritiku odstraňuje.
- `SEARCH TEXT` s diakritikou (CHARSET UTF-8) funguje.
- `SEARCH HEADER References` a `HEADER In-Reply-To` **shodí spojení** (EOF).
  `HEADER Message-ID` funguje, ale skončí chybou „mtd: internal error: Unexpected token“,
  když ID obsahuje `{`. Vlákna se proto sestavují lokálně z hlaviček kandidátů
  nalezených podle slov z předmětu.
- Podporované rozšíření hledání: `SORT`, `I18NLEVEL=1`. `THREAD` ani `ESEARCH` nejsou.
- FROM/TO/SUBJECT se hledají po slovech (každé slovo zvlášť, bez interpunkce jako `[` `]`),
  protože Seznam porovnává celá slova.
- Po `UID MOVE` posílá Seznam `COPYUID` v netagované odpovědi, kterou IMAPClient nevrátí.
  Nové UID se proto dohledává v cílové složce podle Message-ID.
- Hledání podle příloh IMAP nemá: `has_attachment` čte `BODYSTRUCTURE` nejnovějších 500 výsledků.

## Datový model

- `users`: id, email, allowed (allowlist), auth_method, enc_secret (AES-GCM), created_at, last_login_at, last_used_at
- `oauth_clients`: client_id, client_name, client_info (JSON z dynamické registrace)
- `auth_requests`: rozpracované `/authorize` požadavky čekající na přihlašovací formulář (15 min)
- `auth_codes`: autorizační kódy, jednorázové (5 min)
- `tokens`: hashe access (1 h) a refresh (180 dní nečinnosti, rotují) tokenů; vypršené maže server jednou denně
- `login_attempts`: neúspěšná přihlášení pro rate limit (5 na e-mail a 20 na IP za 15 min)

## Kód (HTTP režim)

- `auth.py`: OAuth provider pro MCP SDK (registrace klientů s allowlistem redirect URI, kódy, tokeny).
- `web.py`: stránky `/login`, `/account`, `/`, `/health`.
- `store.py`: databázové operace. `crypto.py`: šifrování a tokeny.
- `app.py`: složení HTTP aplikace a resolver „token → uživatel → schránka“.

## Přihlášení přes Seznam (plánováno)

Seznam má OAuth 2.0 (`login.seznam.cz`) se scope `email`, jehož token jde použít pro
IMAP přes `XOAUTH2` (IMAP server ho nabízí). Uživatelé by pak nemuseli vytvářet hesla
pro aplikace. Je potřeba zaregistrovat službu na vyvojari.seznam.cz. Schéma je na to
připravené (`users.auth_method = seznam_oauth`, v `enc_secret` by byl Seznam refresh token).

## Fáze

1. **Jádro:** IMAP vrstva a nástroje, lokální test přes stdio. *(hotovo, všechny nástroje ověřené na skutečné schránce)*
2. **OAuth a více uživatelů:** DB, šifrování, přihlašovací stránka, allowlist, tokeny. *(hotovo, ověřeno proti skutečnému Seznamu)*
3. **Nasazení:** systemd, reverse proxy, TLS, test v Claude.ai a ChatGPT. *(hotovo, připojení z Claude.ai ověřené)*
4. **Doladění:** stránka „Můj účet“, rate limity, návod pro uživatele, monitoring.
5. **Přílohy, třídění a odesílání:** `read_attachment`, `mark_emails`, `move_emails`, `trash_emails`, `send_email`, `send_draft`.
