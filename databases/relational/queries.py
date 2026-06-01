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

# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    sql = """
        SELECT s.schedule_id, s.line, s.service_type, s.direction,
               s.first_train_time, s.last_train_time, s.frequency_min,
               s.standard_base_fare_usd, s.standard_per_stop_rate,
               s.first_base_fare_usd, s.first_per_stop_rate,
               orig.stop_order AS origin_order,
               dest.stop_order AS destination_order,
               (dest.stop_order - orig.stop_order) AS stops_travelled
        FROM national_rail_schedules s
        JOIN national_rail_schedule_stops orig
            ON orig.schedule_id = s.schedule_id AND orig.station_id = %s
        JOIN national_rail_schedule_stops dest
            ON dest.schedule_id = s.schedule_id AND dest.station_id = %s
        WHERE orig.stop_order < dest.stop_order
        ORDER BY s.schedule_id
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (origin_id, destination_id))
            rows = [dict(r) for r in cur.fetchall()]
            if travel_date and rows:
                for row in rows:
                    cur.execute("""
                        SELECT COUNT(*) AS booked_seats
                        FROM national_rail_bookings
                        WHERE schedule_id = %s AND travel_date = %s AND status != 'cancelled'
                    """, (row["schedule_id"], travel_date))
                    row["booked_seats"] = cur.fetchone()["booked_seats"]
            return rows


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if fare_class == "first":
                cur.execute("""
                    SELECT first_base_fare_usd AS base_fare_usd,
                           first_per_stop_rate AS per_stop_rate_usd
                    FROM national_rail_schedules WHERE schedule_id = %s
                """, (schedule_id,))
            else:
                cur.execute("""
                    SELECT standard_base_fare_usd AS base_fare_usd,
                           standard_per_stop_rate AS per_stop_rate_usd
                    FROM national_rail_schedules WHERE schedule_id = %s
                """, (schedule_id,))
            row = cur.fetchone()
            if not row:
                return None
            base = float(row["base_fare_usd"] or 0)
            per_stop = float(row["per_stop_rate_usd"] or 0)
            return {
                "fare_class": fare_class,
                "base_fare_usd": base,
                "per_stop_rate_usd": per_stop,
                "total_fare_usd": round(base + per_stop * stops_travelled, 2),
            }


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    sql = """
        SELECT s.schedule_id, s.line, s.direction,
               s.first_train_time, s.last_train_time, s.frequency_min,
               s.base_fare_usd, s.per_stop_rate_usd,
               orig.stop_order AS origin_order,
               dest.stop_order AS destination_order,
               (dest.stop_order - orig.stop_order) AS stops_travelled
        FROM metro_schedules s
        JOIN metro_schedule_stops orig
            ON orig.schedule_id = s.schedule_id AND orig.station_id = %s
        JOIN metro_schedule_stops dest
            ON dest.schedule_id = s.schedule_id AND dest.station_id = %s
        WHERE orig.stop_order < dest.stop_order
        ORDER BY s.schedule_id
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (origin_id, destination_id))
            return [dict(r) for r in cur.fetchall()]


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT base_fare_usd, per_stop_rate_usd
                FROM metro_schedules WHERE schedule_id = %s
            """, (schedule_id,))
            row = cur.fetchone()
            if not row:
                return None
            base = float(row["base_fare_usd"])
            per_stop = float(row["per_stop_rate_usd"])
            return {
                "base_fare_usd": base,
                "per_stop_rate_usd": per_stop,
                "total_fare_usd": round(base + per_stop * stops_travelled, 2),
            }


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
) -> list[dict]:
    sql = """
        SELECT s.seat_id, s.coach, s.row, s.col AS column
        FROM seats s
        JOIN seat_layouts l ON l.layout_id = s.layout_id
        WHERE l.schedule_id = %s AND s.fare_class = %s
          AND s.seat_id NOT IN (
              SELECT seat_id FROM national_rail_bookings
              WHERE schedule_id = %s AND travel_date = %s AND status != 'cancelled'
          )
        ORDER BY s.row, s.col
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
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (user_email,))
            row = cur.fetchone()
            return dict(row) if row else None


def query_user_bookings(user_email: str) -> dict:
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (user_email,))
            user = cur.fetchone()
            if not user:
                return {"national_rail": [], "metro": []}
            user_id = user["user_id"]

            cur.execute("""
                SELECT b.booking_id, b.travel_date, b.departure_time::text,
                       b.ticket_type, b.fare_class, b.coach, b.seat_id,
                       b.stops_travelled, b.amount_usd, b.status,
                       b.booked_at, b.travelled_at, b.schedule_id,
                       b.origin_station_id, b.destination_station_id,
                       orig.name AS origin_name, dest.name AS destination_name
                FROM national_rail_bookings b
                JOIN national_rail_stations orig ON orig.station_id = b.origin_station_id
                JOIN national_rail_stations dest ON dest.station_id = b.destination_station_id
                WHERE b.user_id = %s
                ORDER BY b.travel_date DESC
            """, (user_id,))
            nr = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT t.trip_id, t.travel_date, t.ticket_type,
                       t.stops_travelled, t.amount_usd, t.status,
                       t.purchased_at, t.travelled_at, t.schedule_id,
                       t.origin_station_id, t.destination_station_id,
                       orig.name AS origin_name, dest.name AS destination_name
                FROM metro_travels t
                JOIN metro_stations orig ON orig.station_id = t.origin_station_id
                JOIN metro_stations dest ON dest.station_id = t.destination_station_id
                WHERE t.user_id = %s
                ORDER BY t.travel_date DESC
            """, (user_id,))
            metro = [dict(r) for r in cur.fetchall()]

            return {"national_rail": nr, "metro": metro}


def query_payment_info(booking_id: str) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM payments WHERE booking_id = %s ORDER BY paid_at DESC LIMIT 1
            """, (booking_id,))
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
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Calculate stops_travelled from stop orders
            cur.execute("""
                SELECT station_id, stop_order FROM national_rail_schedule_stops
                WHERE schedule_id = %s AND station_id = ANY(%s)
            """, (schedule_id, [origin_station_id, destination_station_id]))
            stops = {r["station_id"]: r["stop_order"] for r in cur.fetchall()}
            if origin_station_id not in stops or destination_station_id not in stops:
                return False, "Origin or destination not on this schedule"
            stops_travelled = abs(stops[destination_station_id] - stops[origin_station_id])

            fare = query_national_rail_fare(schedule_id, fare_class, stops_travelled)
            if not fare:
                return False, "Could not calculate fare"

            # Resolve seat
            if seat_id == "any":
                available = query_available_seats(schedule_id, travel_date, fare_class)
                if not available:
                    return False, "No available seats"
                seat_id = available[0]["seat_id"]
                coach = available[0]["coach"]
            else:
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM national_rail_bookings
                    WHERE schedule_id = %s AND travel_date = %s
                      AND seat_id = %s AND status != 'cancelled'
                """, (schedule_id, travel_date, seat_id))
                if cur.fetchone()["cnt"] > 0:
                    return False, f"Seat {seat_id} is already booked"
                cur.execute("""
                    SELECT s.coach FROM seats s
                    JOIN seat_layouts l ON l.layout_id = s.layout_id
                    WHERE l.schedule_id = %s AND s.seat_id = %s LIMIT 1
                """, (schedule_id, seat_id))
                seat_row = cur.fetchone()
                coach = seat_row["coach"] if seat_row else None

            cur.execute("SELECT first_train_time FROM national_rail_schedules WHERE schedule_id = %s", (schedule_id,))
            sched = cur.fetchone()
            departure_time = sched["first_train_time"] if sched else None

            booking_id = _gen_booking_id()
            now = datetime.now(timezone.utc)

            cur.execute("""
                INSERT INTO national_rail_bookings
                (booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                 travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                 stops_travelled, amount_usd, status, booked_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirmed',%s)
            """, (booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                  travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                  stops_travelled, fare["total_fare_usd"], now))

            payment_id = _gen_payment_id()
            cur.execute("""
                INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
                VALUES (%s, %s, %s, 'credit_card', 'paid', %s)
            """, (payment_id, booking_id, fare["total_fare_usd"], now))

            conn.commit()
            return True, {
                "booking_id": booking_id,
                "schedule_id": schedule_id,
                "origin_station_id": origin_station_id,
                "destination_station_id": destination_station_id,
                "travel_date": travel_date,
                "fare_class": fare_class,
                "seat_id": seat_id,
                "coach": coach,
                "amount_usd": fare["total_fare_usd"],
                "status": "confirmed",
                "payment_id": payment_id,
            }
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT b.*, s.service_type
                FROM national_rail_bookings b
                JOIN national_rail_schedules s ON s.schedule_id = b.schedule_id
                WHERE b.booking_id = %s AND b.user_id = %s
            """, (booking_id, user_id))
            booking = cur.fetchone()
            if not booking:
                return False, "Booking not found or does not belong to this user"
            if booking["status"] == "cancelled":
                return False, "Booking is already cancelled"
            if booking["status"] == "completed":
                return False, "Cannot cancel a completed journey"

            from datetime import date, time as dt_time
            travel_dt = datetime.combine(booking["travel_date"], booking["departure_time"] or dt_time(0, 0))
            travel_dt = travel_dt.replace(tzinfo=timezone.utc)
            hours_before = (travel_dt - datetime.now(timezone.utc)).total_seconds() / 3600

            amount = float(booking["amount_usd"])
            service_type = booking["service_type"]

            if service_type == "express":
                if hours_before >= 48:
                    refund_pct, admin_fee, note = 100, 1.00, "RF002_W1: 100% refund, $1.00 admin fee"
                elif hours_before >= 24:
                    refund_pct, admin_fee, note = 50, 1.00, "RF002_W2: 50% refund, $1.00 admin fee"
                else:
                    refund_pct, admin_fee, note = 0, 0.00, "RF002_W3: No refund"
            else:
                if hours_before >= 48:
                    refund_pct, admin_fee, note = 100, 0.00, "RF001_W1: 100% refund"
                elif hours_before >= 24:
                    refund_pct, admin_fee, note = 75, 0.50, "RF001_W2: 75% refund, $0.50 admin fee"
                elif hours_before >= 2:
                    refund_pct, admin_fee, note = 50, 0.50, "RF001_W3: 50% refund, $0.50 admin fee"
                else:
                    refund_pct, admin_fee, note = 0, 0.00, "RF001_W4: No refund"

            refund_amount = max(0.0, round(amount * refund_pct / 100 - admin_fee, 2))

            cur.execute("UPDATE national_rail_bookings SET status = 'cancelled' WHERE booking_id = %s", (booking_id,))

            if refund_amount > 0:
                cur.execute("""
                    INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
                    VALUES (%s, %s, %s, 'refund', 'refunded', %s)
                """, (_gen_payment_id(), booking_id, refund_amount, datetime.now(timezone.utc)))

            conn.commit()
            return True, {
                "booking_id": booking_id,
                "status": "cancelled",
                "original_amount_usd": amount,
                "refund_amount_usd": refund_amount,
                "policy_applied": note,
            }
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


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
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return False, "Email already registered"

            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            count = cur.fetchone()["cnt"] + 1
            user_id = f"RU{count:02d}"
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            while cur.fetchone():
                count += 1
                user_id = f"RU{count:02d}"
                cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))

            cur.execute("""
                INSERT INTO users (user_id, full_name, email, password, date_of_birth,
                                   secret_question, secret_answer, registered_at, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """, (user_id, f"{first_name} {surname}", email, password,
                  f"{year_of_birth}-01-01", secret_question, secret_answer,
                  datetime.now(timezone.utc)))
            conn.commit()
            return True, user_id
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, email, full_name, phone, date_of_birth, is_active
                FROM users WHERE email = %s AND password = %s
            """, (email, password))
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            parts = row["full_name"].split(" ", 1)
            row["first_name"] = parts[0]
            row["surname"] = parts[1] if len(parts) > 1 else ""
            return row


def get_user_secret_question(email: str) -> Optional[str]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT secret_question FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            return row[0] if row else None


def verify_secret_answer(email: str, answer: str) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT secret_answer FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row:
                return False
            return row[0].strip().lower() == answer.strip().lower()


def update_password(email: str, new_password: str) -> bool:
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password = %s WHERE email = %s", (new_password, email))
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


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
