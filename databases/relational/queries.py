"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
This module handles all queries to PostgreSQL.

TWO ROLES ARE SERVED HERE:
  1. Relational  → dual-network transit (metro + national rail),
                   availability, fares, bookings, seat selection
  2. Vector      → policy document similarity search (pgvector)

STUDENT TASK
------------
Design your schema in databases/relational/schema.sql, seed it with
skeleton/seed_postgres.py, then implement the query functions below.

Functions prefixed with `query_`  are read-only lookups called by the agent.
Functions prefixed with `execute_` are write operations (booking/cancellation).

The vector functions (query_policy_vector_search, store_policy_document)
are already implemented — do not modify them.
"""

from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD


def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"BK-{suffix}"


def _gen_payment_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"PM-{suffix}"


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a cursor, run SQL, return rows.
# Use _connect() for read-only queries; for write operations use a manual
# connection with conn.commit() / conn.rollback() (see execute_booking below).

def example_query() -> dict:
    """Example: returns the name of the connected database."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS db;")
            return dict(cur.fetchone())

# TODO: Implement the query_ and execute_ functions below.
# ─────────────────────────────────────────────────────────────────────────────


# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return national rail schedules that serve both origin and destination stations
    in the correct order, along with seat occupancy for the requested travel date.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        travel_date:     e.g. "2025-06-01" — used to count bookings; omit for general info
    """
    sql = """
        SELECT
            s.schedule_id,
            s.line,
            s.service_type,
            s.direction,
            s.first_train_time::text,
            s.last_train_time::text,
            s.frequency_min,
            s.std_base_fare_usd,
            s.std_per_stop_rate_usd,
            s.first_base_fare_usd,
            s.first_per_stop_rate_usd,
            s.stops_in_order,
            orig.name  AS origin_name,
            dest.name  AS destination_name,
            (
                SELECT pos FROM jsonb_array_elements_text(s.stops_in_order)
                WITH ORDINALITY arr(station, pos)
                WHERE station = %s LIMIT 1
            ) AS origin_pos,
            (
                SELECT pos FROM jsonb_array_elements_text(s.stops_in_order)
                WITH ORDINALITY arr(station, pos)
                WHERE station = %s LIMIT 1
            ) AS destination_pos
        FROM national_rail_schedules s
        JOIN national_rail_stations orig ON orig.station_id = %s
        JOIN national_rail_stations dest ON dest.station_id = %s
        WHERE s.stops_in_order @> %s::jsonb
          AND s.stops_in_order @> %s::jsonb
    """
    origin_json = f'["{origin_id}"]'
    dest_json = f'["{destination_id}"]'
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (
                origin_id, destination_id,
                origin_id, destination_id,
                origin_json, dest_json,
            ))
            rows = [dict(r) for r in cur.fetchall()]

    # Keep only schedules where origin appears before destination
    results = []
    for row in rows:
        if row["origin_pos"] is not None and row["destination_pos"] is not None:
            if row["origin_pos"] < row["destination_pos"]:
                stops = row["stops_in_order"]
                if isinstance(stops, str):
                    import json as _j; stops = _j.loads(stops)
                o_idx = stops.index(origin_id)
                d_idx = stops.index(destination_id)
                row["stops_travelled"] = d_idx - o_idx
                row["stops_in_order"] = stops

                # Seat occupancy for travel_date
                if travel_date:
                    with _connect() as conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute(
                                "SELECT COUNT(*) FROM national_rail_bookings "
                                "WHERE schedule_id = %s AND travel_date = %s "
                                "AND status != 'cancelled'",
                                (row["schedule_id"], travel_date),
                            )
                            row["booked_seats"] = cur2.fetchone()[0]
                    with _connect() as conn3:
                        with conn3.cursor() as cur3:
                            cur3.execute(
                                "SELECT COUNT(*) FROM seat_layouts WHERE schedule_id = %s",
                                (row["schedule_id"],),
                            )
                            row["total_seats"] = cur3.fetchone()[0]
                    row["available_seats"] = row["total_seats"] - row["booked_seats"]

                results.append(row)
    return results


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """
    Calculate the fare for a national rail journey.

    Args:
        schedule_id:     e.g. "NR_SCH01"
        fare_class:      "standard" or "first"
        stops_travelled: number of stops between origin and destination (inclusive)

    Returns:
        dict with fare_class, base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    sql = """
        SELECT std_base_fare_usd, std_per_stop_rate_usd,
               first_base_fare_usd, first_per_stop_rate_usd
        FROM national_rail_schedules
        WHERE schedule_id = %s
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id,))
            row = cur.fetchone()
    if not row:
        return None
    if fare_class == "first":
        base = float(row["first_base_fare_usd"] or 0)
        rate = float(row["first_per_stop_rate_usd"] or 0)
    else:
        base = float(row["std_base_fare_usd"] or 0)
        rate = float(row["std_per_stop_rate_usd"] or 0)
    total = round(base + rate * stops_travelled, 2)
    return {
        "fare_class": fare_class,
        "base_fare_usd": base,
        "per_stop_rate_usd": rate,
        "stops_travelled": stops_travelled,
        "total_fare_usd": total,
    }


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """
    Return metro schedules that serve both origin and destination in the correct order.

    Args:
        origin_id:       e.g. "MS01"
        destination_id:  e.g. "MS09"
    """
    sql = """
        SELECT
            s.schedule_id,
            s.line,
            s.direction,
            s.first_train_time::text,
            s.last_train_time::text,
            s.frequency_min,
            s.base_fare_usd,
            s.per_stop_rate_usd,
            s.stops_in_order,
            orig.name AS origin_name,
            dest.name AS destination_name,
            (
                SELECT pos FROM jsonb_array_elements_text(s.stops_in_order)
                WITH ORDINALITY arr(station, pos)
                WHERE station = %s LIMIT 1
            ) AS origin_pos,
            (
                SELECT pos FROM jsonb_array_elements_text(s.stops_in_order)
                WITH ORDINALITY arr(station, pos)
                WHERE station = %s LIMIT 1
            ) AS destination_pos
        FROM metro_schedules s
        JOIN metro_stations orig ON orig.station_id = %s
        JOIN metro_stations dest ON dest.station_id = %s
        WHERE s.stops_in_order @> %s::jsonb
          AND s.stops_in_order @> %s::jsonb
    """
    origin_json = f'["{origin_id}"]'
    dest_json = f'["{destination_id}"]'
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (
                origin_id, destination_id,
                origin_id, destination_id,
                origin_json, dest_json,
            ))
            rows = [dict(r) for r in cur.fetchall()]

    results = []
    for row in rows:
        if row["origin_pos"] is not None and row["destination_pos"] is not None:
            if row["origin_pos"] < row["destination_pos"]:
                stops = row["stops_in_order"]
                if isinstance(stops, str):
                    import json as _j; stops = _j.loads(stops)
                o_idx = stops.index(origin_id)
                d_idx = stops.index(destination_id)
                row["stops_travelled"] = d_idx - o_idx
                row["stops_in_order"] = stops
                results.append(row)
    return results


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """
    Calculate the metro fare for a single-ticket journey.

    Args:
        schedule_id:     e.g. "MS_SCH01"
        stops_travelled: number of stops between origin and destination

    Returns:
        dict with base_fare_usd, per_stop_rate_usd, total_fare_usd
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT base_fare_usd, per_stop_rate_usd FROM metro_schedules WHERE schedule_id = %s",
                (schedule_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    base = float(row["base_fare_usd"])
    rate = float(row["per_stop_rate_usd"])
    total = round(base + rate * stops_travelled, 2)
    return {
        "base_fare_usd": base,
        "per_stop_rate_usd": rate,
        "stops_travelled": stops_travelled,
        "total_fare_usd": total,
    }


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    """
    Return available seats for a national rail journey on a given date.

    Args:
        schedule_id:  e.g. "NR_SCH01"
        travel_date:  e.g. "2025-06-01"
        fare_class:   "standard" or "first"

    Returns:
        List of dicts: {seat_id, coach, row, column}
    """
    sql = """
        SELECT sl.seat_id, sl.coach, sl.row, sl.col AS column
        FROM seat_layouts sl
        WHERE sl.schedule_id = %s
          AND sl.fare_class = %s
          AND sl.seat_id NOT IN (
              SELECT seat_id FROM national_rail_bookings
              WHERE schedule_id = %s
                AND travel_date = %s
                AND status != 'cancelled'
                AND seat_id IS NOT NULL
          )
        ORDER BY sl.row, sl.col
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (schedule_id, fare_class, schedule_id, travel_date))
            return [dict(r) for r in cur.fetchall()]


def auto_select_adjacent_seats(available_seats: list[dict], count: int) -> list[str]:
    """
    Select `count` seats that are as close together as possible (same row preferred,
    then adjacent rows). Returns a list of seat_ids.

    Args:
        available_seats: output of query_available_seats()
        count:           number of seats needed
    """
    if not available_seats or count <= 0:
        return []
    if count >= len(available_seats):
        return [s["seat_id"] for s in available_seats[:count]]

    from collections import defaultdict
    rows: dict[int, list[dict]] = defaultdict(list)
    for seat in available_seats:
        rows[seat["row"]].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: s[0]["row"]):
        if len(row_seats) >= count:
            return [s["seat_id"] for s in row_seats[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["row"], s["column"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


# ── USER & BOOKING QUERIES ────────────────────────────────────────────────────

def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, full_name, "
                "split_part(full_name, ' ', 1) AS first_name, "
                "split_part(full_name, ' ', 2) AS surname, "
                "phone, date_of_birth::text, is_active "
                "FROM users WHERE email = %s",
                (user_email,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).

    Returns:
        dict with keys 'national_rail' (list) and 'metro' (list)
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # National rail bookings
            cur.execute("""
                SELECT b.booking_id, b.travel_date::text, b.departure_time::text,
                       b.ticket_type, b.fare_class, b.coach, b.seat_id,
                       b.stops_travelled, b.amount_usd, b.status,
                       b.booked_at::text,
                       orig.name AS origin_name, dest.name AS destination_name,
                       s.line, s.service_type
                FROM national_rail_bookings b
                JOIN national_rail_stations orig ON orig.station_id = b.origin_station_id
                JOIN national_rail_stations dest ON dest.station_id = b.destination_station_id
                JOIN national_rail_schedules s ON s.schedule_id = b.schedule_id
                JOIN users u ON u.user_id = b.user_id
                WHERE u.email = %s
                ORDER BY b.travel_date DESC
            """, (user_email,))
            rail = [dict(r) for r in cur.fetchall()]

            # Metro travel history
            cur.execute("""
                SELECT t.trip_id, t.travel_date::text, t.ticket_type,
                       t.stops_travelled, t.amount_usd, t.status,
                       t.purchased_at::text, t.travelled_at::text,
                       orig.name AS origin_name, dest.name AS destination_name,
                       s.line
                FROM metro_travels t
                JOIN metro_stations orig ON orig.station_id = t.origin_station_id
                JOIN metro_stations dest ON dest.station_id = t.destination_station_id
                JOIN metro_schedules s ON s.schedule_id = t.schedule_id
                JOIN users u ON u.user_id = t.user_id
                WHERE u.email = %s
                ORDER BY t.travel_date DESC
            """, (user_email,))
            metro = [dict(r) for r in cur.fetchall()]

    return {"national_rail": rail, "metro": metro}


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT payment_id, booking_id, amount_usd, method, status, paid_at::text "
                "FROM payments WHERE booking_id = %s",
                (booking_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


# ── TRANSACTIONAL OPERATIONS ──────────────────────────────────────────────────

def execute_booking(
    user_id: str,
    schedule_id: str,
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    fare_class: str,
    seat_id: str,
    ticket_type: str = "single",
) -> tuple[bool, dict | str]:
    """
    Create a national rail booking for a logged-in user.

    Args:
        user_id:                e.g. "RU01" — must match the logged-in user
        schedule_id:            e.g. "NR_SCH01"
        origin_station_id:      e.g. "NR01"
        destination_station_id: e.g. "NR05"
        travel_date:            e.g. "2025-06-01"
        fare_class:             "standard" or "first"
        seat_id:                e.g. "B05" (or "any" to auto-assign)
        ticket_type:            "single" (default) or "return"

    Returns:
        (True, booking_dict)   on success
        (False, error_message) on failure
    """
    raise NotImplementedError("TODO: implement after designing your schema")


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.

    Calculates the refund amount according to the booking's service type:
      - Normal service: RF001 windows (100% / 75% / 50% / 0%)
      - Express service: RF002 windows (100% / 50% / 0%)

    Args:
        booking_id: e.g. "BK001"
        user_id:    must match the booking's user_id

    Returns:
        (True, result_dict)  with refund_amount_usd and policy note
        (False, error_msg)
    """
    raise NotImplementedError("TODO: implement after designing your schema")


# ── AUTHENTICATION QUERIES ────────────────────────────────────────────────────

def register_user(
    email: str,
    first_name: str,
    surname: str,
    year_of_birth: int,
    password: str,
    secret_question: str,
    secret_answer: str,
) -> tuple[bool, str]:
    """
    Register a new user.
    Returns (True, user_id) on success or (False, error_message) on failure.

    NOTE: passwords are stored as plain text here intentionally for teaching
    purposes. In production, replace with a salted hash (e.g. bcrypt).
    """
    raise NotImplementedError("TODO: implement after designing your schema")


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials. Returns a user dict on success or None on failure.
    Dict keys: user_id, email, full_name, first_name, surname, phone, date_of_birth, is_active.
    """
    raise NotImplementedError("TODO: implement after designing your schema")


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email, or None if not found."""
    raise NotImplementedError("TODO: implement after designing your schema")


def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored secret answer (case-insensitive)."""
    raise NotImplementedError("TODO: implement after designing your schema")


def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user. Returns True if the row was updated."""
    raise NotImplementedError("TODO: implement after designing your schema")


# ── VECTOR / RAG QUERIES — do not modify ─────────────────────────────────────

def query_policy_vector_search(embedding: list[float], top_k: int = VECTOR_TOP_K) -> list[dict]:
    """
    Find the most relevant policy documents for a given query embedding.

    Args:
        embedding: Query vector from llm.embed(user_question)
        top_k:     Number of results to return

    Returns:
        List of dicts with title, category, content, and similarity score
    """
    sql = """
        SELECT
            title,
            category,
            content,
            1 - (embedding <=> %s::vector) AS similarity
        FROM policy_documents
        WHERE 1 - (embedding <=> %s::vector) > %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (vec_str, vec_str, VECTOR_SIMILARITY_THRESHOLD, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]


def store_policy_document(
    title: str,
    category: str,
    content: str,
    embedding: list[float],
    source_file: str = "",
) -> int:
    """
    Insert a policy document with its embedding into the database.
    Used by skeleton/seed_vectors.py — students don't need to call this directly.

    Returns:
        The new document's id
    """
    sql = """
        INSERT INTO policy_documents (title, category, content, embedding, source_file)
        VALUES (%s, %s, %s, %s::vector, %s)
        RETURNING id
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (title, category, content, vec_str, source_file))
            return cur.fetchone()[0]
