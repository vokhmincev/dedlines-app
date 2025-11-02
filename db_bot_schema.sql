-- кто связал телеграм
CREATE TABLE IF NOT EXISTS telegram_link (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  chat_id BIGINT NOT NULL UNIQUE,
  notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- одноразовые токены для привязки
CREATE TABLE IF NOT EXISTS tg_link_token (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  token VARCHAR(64) NOT NULL UNIQUE,
  expires_at TIMESTAMP NOT NULL,
  used BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- очередь событий, которые бот будет забирать
CREATE TABLE IF NOT EXISTS notif_queue (
  id SERIAL PRIMARY KEY,
  type VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  processed_at TIMESTAMP
);
