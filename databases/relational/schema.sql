-- ============================================================
--  TransitFlow PostgreSQL Schema
--  Seed data is loaded separately by: python skeleton/seed_postgres.py
--
--  TWO ROLES:
--    1. Relational  → dual-network transit data you design below
--    2. Vector      → policy documents for RAG (provided — do not modify)
-- ============================================================

-- ============================================================
--  RELATIONAL SCHEMA
-- ============================================================

CREATE TABLE IF NOT EXISTS metro_stations (
    station_id                          VARCHAR(10)  PRIMARY KEY,
    name                                VARCHAR(100) NOT NULL,
    is_interchange_metro                BOOLEAN      DEFAULT FALSE,
    is_interchange_national_rail        BOOLEAN      DEFAULT FALSE,
    interchange_national_rail_station_id VARCHAR(10)
);

CREATE TABLE IF NOT EXISTS national_rail_stations (
    station_id                      VARCHAR(10)  PRIMARY KEY,
    name                            VARCHAR(100) NOT NULL,
    is_interchange_metro            BOOLEAN      DEFAULT FALSE,
    interchange_metro_station_id    VARCHAR(10)
);

CREATE TABLE IF NOT EXISTS metro_schedules (
    schedule_id             VARCHAR(20)  PRIMARY KEY,
    line                    VARCHAR(10)  NOT NULL,
    direction               VARCHAR(20)  NOT NULL,
    origin_station_id       VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    destination_station_id  VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    first_train_time        TIME         NOT NULL,
    last_train_time         TIME         NOT NULL,
    base_fare_usd           NUMERIC(6,2) NOT NULL,
    per_stop_rate_usd       NUMERIC(6,2) NOT NULL,
    frequency_min           INTEGER      NOT NULL
);

CREATE TABLE IF NOT EXISTS metro_schedule_operates_on (
    schedule_id VARCHAR(20) NOT NULL REFERENCES metro_schedules(schedule_id),
    day         VARCHAR(5)  NOT NULL,
    PRIMARY KEY (schedule_id, day)
);

CREATE TABLE IF NOT EXISTS metro_schedule_stops (
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES metro_schedules(schedule_id),
    station_id              VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    stop_order              INTEGER      NOT NULL,
    travel_time_from_origin INTEGER      NOT NULL,
    PRIMARY KEY (schedule_id, station_id)
);

CREATE TABLE IF NOT EXISTS national_rail_schedules (
    schedule_id             VARCHAR(20)  PRIMARY KEY,
    line                    VARCHAR(10)  NOT NULL,
    service_type            VARCHAR(20)  NOT NULL,
    direction               VARCHAR(20)  NOT NULL,
    origin_station_id       VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    destination_station_id  VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    first_train_time        TIME         NOT NULL,
    last_train_time         TIME         NOT NULL,
    frequency_min           INTEGER      NOT NULL,
    standard_base_fare_usd  NUMERIC(6,2),
    standard_per_stop_rate  NUMERIC(6,2),
    first_base_fare_usd     NUMERIC(6,2),
    first_per_stop_rate     NUMERIC(6,2)
);

CREATE TABLE IF NOT EXISTS national_rail_schedule_operates_on (
    schedule_id VARCHAR(20) NOT NULL REFERENCES national_rail_schedules(schedule_id),
    day         VARCHAR(5)  NOT NULL,
    PRIMARY KEY (schedule_id, day)
);

CREATE TABLE IF NOT EXISTS national_rail_schedule_stops (
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES national_rail_schedules(schedule_id),
    station_id              VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    stop_order              INTEGER      NOT NULL,
    travel_time_from_origin INTEGER      NOT NULL,
    PRIMARY KEY (schedule_id, station_id)
);

CREATE TABLE IF NOT EXISTS seat_layouts (
    layout_id   VARCHAR(20) PRIMARY KEY,
    schedule_id VARCHAR(20) NOT NULL REFERENCES national_rail_schedules(schedule_id)
);

CREATE TABLE IF NOT EXISTS seats (
    layout_id  VARCHAR(20) NOT NULL REFERENCES seat_layouts(layout_id),
    coach      VARCHAR(5)  NOT NULL,
    fare_class VARCHAR(20) NOT NULL,
    seat_id    VARCHAR(10) NOT NULL,
    row        INTEGER     NOT NULL,
    col        VARCHAR(5)  NOT NULL,
    PRIMARY KEY (layout_id, seat_id)
);

-- Users (no password stored here — credentials are in user_credentials table)
CREATE TABLE IF NOT EXISTS users (
    user_id         VARCHAR(10)  PRIMARY KEY,
    full_name       VARCHAR(100) NOT NULL,
    email           VARCHAR(150) NOT NULL UNIQUE,
    phone           VARCHAR(20),
    date_of_birth   DATE,
    secret_question TEXT,
    secret_answer   TEXT,
    registered_at   TIMESTAMPTZ,
    is_active       BOOLEAN      DEFAULT TRUE
);

-- User Credentials (separate table — password hash and salt never stored with user info)
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id       VARCHAR(10)  PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    password_hash TEXT         NOT NULL,
    salt          TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS national_rail_bookings (
    booking_id              VARCHAR(20)  PRIMARY KEY,
    user_id                 VARCHAR(10)  NOT NULL REFERENCES users(user_id),
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES national_rail_schedules(schedule_id),
    origin_station_id       VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    destination_station_id  VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    travel_date             DATE         NOT NULL,
    departure_time          TIME,
    ticket_type             VARCHAR(20),
    fare_class              VARCHAR(20),
    coach                   VARCHAR(5),
    seat_id                 VARCHAR(10),
    stops_travelled         INTEGER,
    amount_usd              NUMERIC(8,2),
    status                  VARCHAR(20),
    booked_at               TIMESTAMPTZ,
    travelled_at            TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS metro_travels (
    trip_id                 VARCHAR(20)  PRIMARY KEY,
    user_id                 VARCHAR(10)  NOT NULL REFERENCES users(user_id),
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES metro_schedules(schedule_id),
    origin_station_id       VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    destination_station_id  VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    travel_date             DATE         NOT NULL,
    ticket_type             VARCHAR(20),
    stops_travelled         INTEGER,
    amount_usd              NUMERIC(8,2),
    status                  VARCHAR(20),
    purchased_at            TIMESTAMPTZ,
    travelled_at            TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id  VARCHAR(20) PRIMARY KEY,
    booking_id  VARCHAR(20),
    amount_usd  NUMERIC(8,2),
    method      VARCHAR(30),
    status      VARCHAR(20),
    paid_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id  VARCHAR(20) PRIMARY KEY,
    booking_id   VARCHAR(20),
    user_id      VARCHAR(10) REFERENCES users(user_id),
    rating       INTEGER,
    comment      TEXT,
    submitted_at TIMESTAMPTZ
);




-- ============================================================
--  VECTOR SCHEMA  (RAG / Help Desk) — do not modify
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_documents (
    id          SERIAL       PRIMARY KEY,
    title       VARCHAR(200) NOT NULL,
    category    VARCHAR(50)  NOT NULL,  -- 'refund', 'booking', 'conduct'
    content     TEXT         NOT NULL,
    -- 768-dim  → Ollama nomic-embed-text (default)
    -- 3072-dim → Gemini gemini-embedding-001
    -- If you switch LLM_PROVIDER to gemini, change to vector(3072) and reset the database.
    embedding   vector(768),
    source_file VARCHAR(200),
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- Index for fast cosine similarity search
CREATE INDEX IF NOT EXISTS policy_documents_embedding_idx ON policy_documents USING hnsw (embedding vector_cosine_ops);
