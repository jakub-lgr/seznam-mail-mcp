-- seznam-mail-mcp database schema (postgresql)
-- Generated from src/seznam_mail_mcp/db.py; do not edit by hand.
-- Regenerate: python -m seznam_mail_mcp.db dump-schema --write

CREATE TABLE IF NOT EXISTS login_attempts (
	id BIGSERIAL NOT NULL, 
	email VARCHAR(320), 
	ip VARCHAR(64), 
	success BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_login_attempts_email_created ON login_attempts (email, created_at);

CREATE INDEX IF NOT EXISTS ix_login_attempts_ip_created ON login_attempts (ip, created_at);

CREATE TABLE IF NOT EXISTS oauth_clients (
	client_id VARCHAR(64) NOT NULL, 
	client_name VARCHAR(200), 
	redirect_uris TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (client_id)
);

CREATE TABLE IF NOT EXISTS users (
	id BIGSERIAL NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	enc_password BYTEA, 
	allowed BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS auth_codes (
	code_hash VARCHAR(64) NOT NULL, 
	client_id VARCHAR(64) NOT NULL, 
	user_id BIGINT NOT NULL, 
	redirect_uri TEXT NOT NULL, 
	code_challenge VARCHAR(128) NOT NULL, 
	scope TEXT, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (code_hash), 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tokens (
	token_hash VARCHAR(64) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	user_id BIGINT NOT NULL, 
	client_id VARCHAR(64) NOT NULL, 
	scope TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked BOOLEAN DEFAULT false NOT NULL, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_tokens_user_id ON tokens (user_id);
