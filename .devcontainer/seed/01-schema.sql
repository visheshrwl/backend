-- Seed schema + data for the labs that need persistence.
-- Runs once on first Postgres boot (docker-entrypoint-initdb.d).

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'free',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (id, name, email, plan) VALUES
    (1, 'Ada Lovelace',   'ada@example.com',   'pro'),
    (2, 'Alan Turing',    'alan@example.com',  'free'),
    (3, 'Grace Hopper',   'grace@example.com', 'pro'),
    (4, 'Edsger Dijkstra','edsger@example.com','free'),
    (5, 'Barbara Liskov', 'barbara@example.com','enterprise')
ON CONFLICT (id) DO NOTHING;
