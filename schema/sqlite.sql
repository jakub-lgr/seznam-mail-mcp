-- seznam-mail-mcp database schema (sqlite)
-- Generated from src/seznam_mail_mcp/db.py; do not edit by hand.
-- Regenerate: python -m seznam_mail_mcp.db dump-schema --write

CREATE TABLE IF NOT EXISTS login_attempts (
	id INTEGER NOT NULL, 
	email VARCHAR(320), 
	ip VARCHAR(64), 
	success BOOLEAN NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_login_attempts_email_created ON login_attempts (email, created_at);

CREATE INDEX IF NOT EXISTS ix_login_attempts_ip_created ON login_attempts (ip, created_at);

CREATE TABLE IF NOT EXISTS oauth_clients (
	client_id VARCHAR(128) NOT NULL, 
	client_name VARCHAR(200), 
	client_info TEXT NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (client_id)
);

CREATE TABLE IF NOT EXISTS users (
	id INTEGER NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	allowed BOOLEAN DEFAULT 0 NOT NULL, 
	auth_method VARCHAR(32), 
	enc_secret BLOB, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	last_login_at DATETIME, 
	last_used_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS auth_codes (
	code_hash VARCHAR(64) NOT NULL, 
	client_id VARCHAR(128) NOT NULL, 
	user_id INTEGER NOT NULL, 
	params TEXT NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (code_hash), 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_requests (
	request_hash VARCHAR(64) NOT NULL, 
	client_id VARCHAR(128) NOT NULL, 
	params TEXT NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (request_hash), 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tokens (
	token_hash VARCHAR(64) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	user_id INTEGER NOT NULL, 
	client_id VARCHAR(128) NOT NULL, 
	scopes TEXT DEFAULT '' NOT NULL, 
	resource TEXT, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked BOOLEAN DEFAULT 0 NOT NULL, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_tokens_user_client ON tokens (user_id, client_id);
