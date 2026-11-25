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

Ve vývoji (fáze 2), viz [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
