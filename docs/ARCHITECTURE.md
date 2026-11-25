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
   (30 dní, rotuje se při každém použití).
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
- Ve v1 není odesílání, mazání ani přesouvání. E-maily se čtou přes `BODY.PEEK`,
  takže se neoznačují jako přečtené.
- Uživatel si může na stránce „Můj účet“ všechno smazat. Úplné odříznutí jde
  zrušením hesla pro aplikaci na Seznamu.

## Nástroje (v1)

| Tool | Popis |
|---|---|
| `list_folders` | složky a počty nepřečtených |
| `search_emails` | hledání (odesílatel, předmět, text, datum, složka, nepřečtené) → hlavičky a ID |
| `read_email` | text e-mailu (HTML → text, zkrácený), seznam příloh |
| `read_thread` | celé vlákno |
| `create_draft` | uloží koncept (i odpověď) do Konceptů, neodesílá |

Později a volitelně: `send_email` s potvrzováním nebo whitelistem adresátů.

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

## Datový model (návrh)

- `users`: id, email, enc_password, allowed, created_at, last_used_at
- `oauth_clients`: client_id, name, redirect_uris, created_at
- `auth_codes`: code_hash, client_id, user_id, redirect_uri, code_challenge, expires_at
- `tokens`: token_hash, kind (access/refresh), user_id, client_id, expires_at, revoked
- `login_attempts`: email/ip, timestamp (rate limit)

## Fáze

1. **Jádro:** IMAP vrstva a nástroje, lokální test přes stdio. *(hotovo, všechny nástroje ověřené na skutečné schránce)*
2. **OAuth a více uživatelů:** DB, šifrování, přihlašovací stránka, allowlist, tokeny.
3. **Nasazení:** systemd, reverse proxy, TLS, test v Claude.ai a ChatGPT.
4. **Doladění:** stránka „Můj účet“, rate limity, návod pro uživatele, monitoring.
5. *(volitelně)* Odesílání s pojistkami.
