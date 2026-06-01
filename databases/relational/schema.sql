-- ============================================================
--  TransitFlow PostgreSQL Schema
--  Seed data is loaded separately by: python skeleton/seed_postgres.py
--
--  TWO ROLES:
--    1. Relational  → dual-network transit data you design below
--    2. Vector      → policy documents for RAG (provided — do not modify)
-- ============================================================

-- ============================================================
--  STUDENT TASK — Relational Schema
-- ============================================================

-- 1. Metro Stations
CREATE TABLE IF NOT EXISTS metro_stations (
    station_id                       VARCHAR(10)  PRIMARY KEY,
    name                             TEXT         NOT NULL,
    lines                            JSONB        NOT NULL,
    is_interchange_metro             BOOLEAN      DEFAULT FALSE,
    is_interchange_national_rail     BOOLEAN      DEFAULT FALSE,
    interchange_nr_station_id        VARCHAR(10)
);

-- 2. National Rail Stations
CREATE TABLE IF NOT EXISTS national_rail_stations (
    station_id                       VARCHAR(10)  PRIMARY KEY,
    name                             TEXT         NOT NULL,
    lines                            JSONB        NOT NULL,
    is_interchange_metro             BOOLEAN      DEFAULT FALSE,
    interchange_metro_station_id     VARCHAR(10)
);

-- 3. Metro Schedules
CREATE TABLE IF NOT EXISTS metro_schedules (
    schedule_id                VARCHAR(20)  PRIMARY KEY,
    line                       VARCHAR(5)   NOT NULL,
    direction                  VARCHAR(20),
    origin_station_id          VARCHAR(10)  REFERENCES metro_stations(station_id),
    destination_station_id     VARCHAR(10)  REFERENCES metro_stations(station_id),
    stops_in_order             JSONB        NOT NULL,
    travel_time_from_origin    JSONB,
    first_train_time           TIME,
    last_train_time            TIME,
    base_fare_usd              NUMERIC(6,2) NOT NULL,
    per_stop_rate_usd          NUMERIC(6,2) NOT NULL,
    frequency_min              INTEGER,
    operates_on                JSONB        NOT NULL
);

-- 4. National Rail Schedules
CREATE TABLE IF NOT EXISTS national_rail_schedules (
    schedule_id                VARCHAR(20)  PRIMARY KEY,
    line                       VARCHAR(5)   NOT NULL,
    service_type               VARCHAR(20)  NOT NULL,
    direction                  VARCHAR(20),
    origin_station_id          VARCHAR(10)  REFERENCES national_rail_stations(station_id),
    destination_station_id     VARCHAR(10)  REFERENCES national_rail_stations(station_id),
    stops_in_order             JSONB        NOT NULL,
    travel_time_from_origin    JSONB,
    first_train_time           TIME,
    last_train_time            TIME,
    std_base_fare_usd          NUMERIC(6,2),
    std_per_stop_rate_usd      NUMERIC(6,2),
    first_base_fare_usd        NUMERIC(6,2),
    first_per_stop_rate_usd    NUMERIC(6,2),
    frequency_min              INTEGER,
    operates_on                JSONB        NOT NULL
);

-- 5. Seat Layouts (one row per seat)
CREATE TABLE IF NOT EXISTS seat_layouts (
    seat_id      VARCHAR(10)  NOT NULL,
    schedule_id  VARCHAR(20)  NOT NULL REFERENCES national_rail_schedules(schedule_id),
    coach        VARCHAR(5)   NOT NULL,
    fare_class   VARCHAR(20)  NOT NULL,
    row          INTEGER      NOT NULL,
    col          VARCHAR(5)   NOT NULL,
    PRIMARY KEY (schedule_id, seat_id)
);

-- 6. Users
CREATE TABLE IF NOT EXISTS users (
    user_id         VARCHAR(10)   PRIMARY KEY,
    full_name       TEXT          NOT NULL,
    email           VARCHAR(200)  UNIQUE NOT NULL,
    password        TEXT          NOT NULL,
    phone           VARCHAR(20),
    date_of_birth   DATE,
    secret_question TEXT,
    secret_answer   TEXT,
    registered_at   TIMESTAMPTZ   DEFAULT NOW(),
    is_active       BOOLEAN       DEFAULT TRUE
);

-- 7. National Rail Bookings
CREATE TABLE IF NOT EXISTS national_rail_bookings (
    booking_id              VARCHAR(20)  PRIMARY KEY,
    user_id                 VARCHAR(10)  NOT NULL REFERENCES users(user_id),
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES national_rail_schedules(schedule_id),
    origin_station_id       VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    destination_station_id  VARCHAR(10)  NOT NULL REFERENCES national_rail_stations(station_id),
    travel_date             DATE         NOT NULL,
    departure_time          TIME,
    ticket_type             VARCHAR(20)  NOT NULL,
    fare_class              VARCHAR(20)  NOT NULL,
    coach                   VARCHAR(5),
    seat_id                 VARCHAR(10),
    stops_travelled         INTEGER,
    amount_usd              NUMERIC(8,2) NOT NULL,
    status                  VARCHAR(20)  NOT NULL DEFAULT 'confirmed',
    booked_at               TIMESTAMPTZ  DEFAULT NOW(),
    travelled_at            TIMESTAMPTZ
);

-- 8. Metro Travel History
CREATE TABLE IF NOT EXISTS metro_travels (
    trip_id                 VARCHAR(20)  PRIMARY KEY,
    user_id                 VARCHAR(10)  NOT NULL REFERENCES users(user_id),
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES metro_schedules(schedule_id),
    origin_station_id       VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    destination_station_id  VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id),
    travel_date             DATE         NOT NULL,
    ticket_type             VARCHAR(20)  NOT NULL,
    day_pass_ref            VARCHAR(20),
    stops_travelled         INTEGER,
    amount_usd              NUMERIC(8,2) NOT NULL,
    status                  VARCHAR(20)  NOT NULL DEFAULT 'completed',
    purchased_at            TIMESTAMPTZ  DEFAULT NOW(),
    travelled_at            TIMESTAMPTZ
);

-- 9. Payments (booking_id is polymorphic: BK*** or MT***)
CREATE TABLE IF NOT EXISTS payments (
    payment_id   VARCHAR(20)  PRIMARY KEY,
    booking_id   VARCHAR(20)  NOT NULL,
    amount_usd   NUMERIC(8,2) NOT NULL,
    method       VARCHAR(50),
    status       VARCHAR(20)  NOT NULL DEFAULT 'paid',
    paid_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- 10. Feedback (booking_id is polymorphic: BK*** or MT***)
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id   VARCHAR(20)  PRIMARY KEY,
    booking_id    VARCHAR(20)  NOT NULL,
    user_id       VARCHAR(10)  NOT NULL REFERENCES users(user_id),
    rating        INTEGER      CHECK (rating BETWEEN 1 AND 5),
    comment       TEXT,
    submitted_at  TIMESTAMPTZ  DEFAULT NOW()
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
