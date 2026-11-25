# seznam-mail-mcp

Self-hosted [MCP](https://modelcontextprotocol.io) server, který propojí AI
asistenty (Claude, ChatGPT a další MCP klienty) s e-mailovou schránkou na
[Seznam.cz](https://email.seznam.cz).

- Běží na vlastním serveru, přihlašovací údaje nejdou přes žádnou cizí službu.
- Podporuje víc uživatelů: každý se přihlásí svým e-mailem a heslem pro aplikaci,
  přístup omezuje allowlist.
- Ve výchozím stavu jen čte a vytváří koncepty. Nic neodesílá ani nemaže.

## Nástroje

| Tool | Popis |
|---|---|
| `list_folders` | složky, počty zpráv a nepřečtených |
| `search_emails` | hledání podle odesílatele, příjemce, předmětu, textu, data, nepřečtených |
| `read_email` | text e-mailu a seznam příloh (neoznačí jako přečtený) |
| `read_thread` | celé vlákno včetně odeslaných odpovědí |
| `create_draft` | uloží koncept nebo odpověď do Konceptů (neodesílá) |

## Instalace

```
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Na Seznamu povol v nastavení schránky přístup přes IMAP a vytvoř si **heslo pro aplikaci**.

### Jeden uživatel, lokálně (stdio)

Do `.env` vyplň `SEZNAM_EMAIL` a `SEZNAM_APP_PASSWORD` a přidej server do klienta,
například do Claude Code:

```
claude mcp add seznam-mail -- /cesta/k/projektu/.venv/bin/seznam-mail-mcp --env-file /cesta/k/projektu/.env stdio
```

### Více uživatelů přes HTTPS (OAuth)

Server má vlastní OAuth, takže ho jde přidat jako vlastní konektor do Claude.ai, ChatGPT
a dalších klientů. Každý uživatel se přihlásí na přihlašovací stránce serveru svým
e-mailem a heslem pro aplikace. Přístup mají jen adresy na allowlistu.

1. V `.env` nastav aspoň `PUBLIC_URL` (veřejná HTTPS adresa) a `PORT`, viz [.env.example](.env.example).
2. Povol uživatele:
   ```
   seznam-mail-mcp users allow jmeno@seznam.cz
   seznam-mail-mcp users list
   ```
3. Spusť server za reverse proxy s TLS. Vzory jsou v [deploy/](deploy/): systemd služba
   [seznam-mail-mcp.service](deploy/seznam-mail-mcp.service) a nginx [nginx.conf.example](deploy/nginx.conf.example).
   Ručně: `seznam-mail-mcp serve`.
4. V klientovi přidej konektor s adresou `https://<tvoje-domena>/mcp`.

Další příkazy: `users deny` (zakáže adresu a odhlásí její aplikace) a `users delete`.
Uživatel se může sám odpojit na stránce `/account`.

Šifrovací klíč pro uložená hesla se při prvním startu vytvoří v `data/secret.key`.
**Zálohuj ho**, bez něj uložená hesla nejdou přečíst (uživatelé by se museli přihlásit znovu).

## Databáze

Funguje s **PostgreSQL** i **SQLite**. Bez nastavení `DATABASE_URL` si server sám
vytvoří SQLite soubor ve složce `data/`. Pro PostgreSQL nastav v `.env`:

```
DATABASE_URL=postgresql://uzivatel:heslo@127.0.0.1:5432/seznam_mail_mcp
```

Schéma pro ruční vytvoření je ve [schema/](schema/). Tabulky ale vytvoří i sám server:

```
python -m seznam_mail_mcp.db init
```

Vzor konfigurace je v [.env.example](.env.example).

> Stav: ve vývoji. Návrh je v [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
