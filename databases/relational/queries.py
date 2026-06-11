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

import bcrypt
import psycopg2
import psycopg2.extras

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD


def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


# Register a global type caster: NUMERIC/DECIMAL columns come back as float,
# not Decimal — so all results are directly JSON-serialisable without extra steps.
_DEC2FLOAT = psycopg2.extensions.new_type(
    psycopg2.extensions.DECIMAL.values,
    "DEC2FLOAT",
    lambda value, curs: float(value) if value is not None else None,
)
psycopg2.extensions.register_type(_DEC2FLOAT)


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
    # Join stops table twice (once for origin, once for destination) so we can
    # compare stop_order directly — avoids JSONB array scanning.
    # WHERE o.stop_order < d.stop_order ensures direction is correct.
    #
    # When a travel_date is given, also filter by operating day: NR_SCH05–08
    # run weekdays only, so e.g. a Saturday query must exclude them and may
    # legitimately return []. to_char(date, 'dy') yields lowercase 'mon'..'sun',
    # matching the day values stored in national_rail_schedule_operates_on.
    day_filter = """
          AND EXISTS (
              SELECT 1 FROM national_rail_schedule_operates_on op
              WHERE op.schedule_id = s.schedule_id
                AND op.day = to_char(%s::date, 'dy')
          )
    """ if travel_date else ""
    sql = f"""
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
            orig_s.name  AS origin_name,
            dest_s.name  AS destination_name,
            o.stop_order AS origin_stop_order,
            d.stop_order AS destination_stop_order,
            (d.stop_order - o.stop_order) AS stops_travelled
        FROM national_rail_schedules s
        JOIN national_rail_schedule_stops o ON o.schedule_id = s.schedule_id AND o.station_id = %s
        JOIN national_rail_schedule_stops d ON d.schedule_id = s.schedule_id AND d.station_id = %s
        JOIN national_rail_stations orig_s ON orig_s.station_id = %s
        JOIN national_rail_stations dest_s ON dest_s.station_id = %s
        WHERE o.stop_order < d.stop_order
        {day_filter}
        ORDER BY s.first_train_time
    """
    params = [origin_id, destination_id, origin_id, destination_id]
    if travel_date:
        params.append(travel_date)
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]

    # Augment each schedule with seat availability for the requested date
    for row in rows:
        if travel_date:
            with _connect() as conn2:
                with conn2.cursor() as cur2:
                    # Count non-cancelled bookings to determine occupied seats
                    cur2.execute(
                        "SELECT COUNT(*) FROM national_rail_bookings "
                        "WHERE schedule_id = %s AND travel_date = %s AND status != 'cancelled'",
                        (row["schedule_id"], travel_date),
                    )
                    row["booked_seats"] = cur2.fetchone()[0]
                    cur2.execute(
                        "SELECT COUNT(*) FROM seat_layouts WHERE schedule_id = %s",
                        (row["schedule_id"],),
                    )
                    row["total_seats"] = cur2.fetchone()[0]
            row["available_seats"] = row["total_seats"] - row["booked_seats"]

    return rows


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
    # Same double-join pattern as national rail — compare stop_order from junction table
    # to confirm origin comes before destination on this schedule.
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
            orig_s.name AS origin_name,
            dest_s.name AS destination_name,
            (d.stop_order - o.stop_order) AS stops_travelled,
            -- Full ordered stop sequence of this schedule: results must include
            -- the stop sequence itself, not just the count of stops travelled.
            (SELECT array_agg(st.station_id ORDER BY st.stop_order)
             FROM metro_schedule_stops st
             WHERE st.schedule_id = s.schedule_id) AS stops_in_order
        FROM metro_schedules s
        JOIN metro_schedule_stops o ON o.schedule_id = s.schedule_id AND o.station_id = %s
        JOIN metro_schedule_stops d ON d.schedule_id = s.schedule_id AND d.station_id = %s
        JOIN metro_stations orig_s ON orig_s.station_id = %s
        JOIN metro_stations dest_s ON dest_s.station_id = %s
        WHERE o.stop_order < d.stop_order
        ORDER BY s.line
    """
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (origin_id, destination_id, origin_id, destination_id))
            return [dict(r) for r in cur.fetchall()]


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

def _norm_email(email: str) -> str:
    """Normalise an email for storage and lookup.

    Emails are stored lowercase at registration, so every lookup must lower
    its input too — VARCHAR comparison is case-sensitive, and a user who
    registers as Alice@Example.com would otherwise become invisible to
    profile/booking lookups made with the lowercased session state.
    """
    return email.strip().lower()


def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email, or None if not found."""
    user_email = _norm_email(user_email)
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, full_name, "
                "split_part(full_name, ' ', 1) AS first_name, "
                "split_part(full_name, ' ', 2) AS surname, "
                "phone, date_of_birth::text, "
                # Extract year from date_of_birth for live testing requirement
                "EXTRACT(YEAR FROM date_of_birth)::int AS year_of_birth, "
                "is_active "
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
    user_email = _norm_email(user_email)
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
            # A cancelled booking has two payment rows (purchase + refund);
            # order by paid_at so we deterministically return the original purchase.
            cur.execute(
                "SELECT payment_id, booking_id, amount_usd, method, status, paid_at::text "
                "FROM payments WHERE booking_id = %s "
                "ORDER BY paid_at ASC",
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
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Use junction table to get stop positions — avoids JSONB scanning
            cur.execute("""
                SELECT
                    s.std_base_fare_usd, s.std_per_stop_rate_usd,
                    s.first_base_fare_usd, s.first_per_stop_rate_usd,
                    s.first_train_time, s.service_type,
                    o.stop_order AS origin_idx,
                    d.stop_order AS dest_idx
                FROM national_rail_schedules s
                JOIN national_rail_schedule_stops o
                    ON o.schedule_id = s.schedule_id AND o.station_id = %s
                JOIN national_rail_schedule_stops d
                    ON d.schedule_id = s.schedule_id AND d.station_id = %s
                WHERE s.schedule_id = %s
            """, (origin_station_id, destination_station_id, schedule_id))
            sched = cur.fetchone()
            if not sched:
                return False, "Schedule not found or stations not on this schedule"
            if sched["origin_idx"] >= sched["dest_idx"]:
                return False, "Origin must come before destination on this schedule"

            stops_travelled = sched["dest_idx"] - sched["origin_idx"]

            if fare_class == "first":
                base = float(sched["first_base_fare_usd"] or 0)
                per_stop = float(sched["first_per_stop_rate_usd"] or 0)
            else:
                base = float(sched["std_base_fare_usd"] or 0)
                per_stop = float(sched["std_per_stop_rate_usd"] or 0)
            total_fare = round(base + per_stop * stops_travelled, 2)

            # Resolve seat
            if seat_id == "any":
                cur.execute("""
                    SELECT seat_id, coach FROM seat_layouts
                    WHERE schedule_id = %s AND fare_class = %s
                      AND seat_id NOT IN (
                          SELECT seat_id FROM national_rail_bookings
                          WHERE schedule_id = %s AND travel_date = %s AND status != 'cancelled'
                      )
                    ORDER BY row, col LIMIT 1
                """, (schedule_id, fare_class, schedule_id, travel_date))
                seat_row = cur.fetchone()
                if not seat_row:
                    return False, "No available seats"
                seat_id = seat_row["seat_id"]
                coach = seat_row["coach"]
            else:
                # Validate the seat physically exists on this schedule in the
                # requested fare class BEFORE checking occupancy — otherwise a
                # typo'd seat_id would create a ghost booking with coach = NULL.
                cur.execute("SELECT coach, fare_class FROM seat_layouts "
                            "WHERE schedule_id = %s AND seat_id = %s",
                            (schedule_id, seat_id))
                seat_row = cur.fetchone()
                if not seat_row:
                    return False, f"Seat {seat_id} does not exist on schedule {schedule_id}"
                if seat_row["fare_class"] != fare_class:
                    return False, (f"Seat {seat_id} is a {seat_row['fare_class']}-class seat; "
                                   f"requested fare class is {fare_class}")
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM national_rail_bookings
                    WHERE schedule_id = %s AND travel_date = %s
                      AND seat_id = %s AND status != 'cancelled'
                """, (schedule_id, travel_date, seat_id))
                if cur.fetchone()["cnt"] > 0:
                    return False, f"Seat {seat_id} is already booked"
                coach = seat_row["coach"]

            booking_id = _gen_booking_id()
            now = datetime.now(timezone.utc)

            # Design limitation: the timetable is frequency-based, so no exact
            # service time is chosen at booking. first_train_time is stored as a
            # representative departure_time; execute_cancellation derives its
            # refund window from this value, which is therefore conservative.
            cur.execute("""
                INSERT INTO national_rail_bookings
                (booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                 travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                 stops_travelled, amount_usd, status, booked_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirmed',%s)
            """, (booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                  travel_date, sched["first_train_time"], ticket_type, fare_class, coach,
                  seat_id, stops_travelled, total_fare, now))

            payment_id = _gen_payment_id()
            cur.execute("""
                INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
                VALUES (%s, %s, %s, 'credit_card', 'paid', %s)
            """, (payment_id, booking_id, total_fare, now))

            conn.commit()
            return True, {
                "booking_id": booking_id,
                "user_id": user_id,
                "schedule_id": schedule_id,
                "origin_station_id": origin_station_id,
                "destination_station_id": destination_station_id,
                "travel_date": travel_date,
                "fare_class": fare_class,
                "seat_id": seat_id,
                "coach": coach,
                "amount_usd": total_fare,
                "status": "confirmed",
                "payment_id": payment_id,
            }
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking (soft delete: status → 'cancelled') and
    calculate the refund per the cancellation windows in refund_policy.json
    (RF001 for normal services, RF002 for express).

    Args:
        booking_id: e.g. "BK-A1B2C3" — must belong to user_id
        user_id:    e.g. "RU01" — the logged-in user

    Returns:
        (True, result_dict)    on success — includes refund_amount
        (False, error_message) if not found, not owned, or already cancelled
    """
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

            from datetime import time as dt_time
            travel_dt = datetime.combine(booking["travel_date"],
                                         booking["departure_time"] or dt_time(0, 0))
            travel_dt = travel_dt.replace(tzinfo=timezone.utc)
            hours_before = (travel_dt - datetime.now(timezone.utc)).total_seconds() / 3600

            amount = float(booking["amount_usd"])
            if booking["service_type"] == "express":
                if hours_before >= 48:
                    pct, fee, note = 100, 1.00, "RF002_W1: 100% refund, $1.00 admin fee"
                elif hours_before >= 24:
                    pct, fee, note = 50, 1.00, "RF002_W2: 50% refund, $1.00 admin fee"
                else:
                    pct, fee, note = 0, 0.00, "RF002_W3: No refund"
            else:
                if hours_before >= 48:
                    pct, fee, note = 100, 0.00, "RF001_W1: 100% refund"
                elif hours_before >= 24:
                    pct, fee, note = 75, 0.50, "RF001_W2: 75% refund, $0.50 admin fee"
                elif hours_before >= 2:
                    pct, fee, note = 50, 0.50, "RF001_W3: 50% refund, $0.50 admin fee"
                else:
                    pct, fee, note = 0, 0.00, "RF001_W4: No refund"

            refund = max(0.0, round(amount * pct / 100 - fee, 2))

            cur.execute("UPDATE national_rail_bookings SET status = 'cancelled' WHERE booking_id = %s",
                        (booking_id,))
            if refund > 0:
                cur.execute("""
                    INSERT INTO payments (payment_id, booking_id, amount_usd, method, status, paid_at)
                    VALUES (%s, %s, %s, 'refund', 'refunded', %s)
                """, (_gen_payment_id(), booking_id, refund, datetime.now(timezone.utc)))

            conn.commit()
            return True, {
                "booking_id": booking_id,
                "status": "cancelled",
                "original_amount_usd": amount,
                # Both keys carry the same value: refund_amount is the contract
                # name expected by callers; refund_amount_usd kept for clarity.
                "refund_amount": refund,
                "refund_amount_usd": refund,
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
    """
    Register a new user.
    Password is hashed with bcrypt; hash and salt stored in user_credentials (not in users).
    Returns (True, user_id) on success or (False, error_message) on failure.
    """
    email = _norm_email(email)   # stored lowercase so later lookups always match
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check email uniqueness
            cur.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return False, "Email already registered"

            # Generate unique user_id
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            count = cur.fetchone()["cnt"] + 1
            user_id = f"RU{count:02d}"
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            while cur.fetchone():
                count += 1
                user_id = f"RU{count:02d}"
                cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))

            # Insert user profile (no credential material here — only the
            # secret QUESTION, which is public by design)
            cur.execute("""
                INSERT INTO users (user_id, full_name, email, date_of_birth,
                                   secret_question, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
            """, (user_id, f"{first_name} {surname}", email,
                  f"{year_of_birth}-01-01", secret_question))

            # Hash password AND secret answer into user_credentials.
            # The secret answer can reset the password, so it is a credential:
            # storing it in plain text would be an account-takeover backdoor.
            # Normalise (trim + lowercase) before hashing so the comparison in
            # verify_secret_answer stays case-insensitive.
            salt = bcrypt.gensalt()
            password_hash = bcrypt.hashpw(password.encode(), salt)
            answer_hash = bcrypt.hashpw(
                secret_answer.strip().lower().encode(), bcrypt.gensalt())
            cur.execute("""
                INSERT INTO user_credentials (user_id, password_hash, salt, secret_answer_hash)
                VALUES (%s, %s, %s, %s)
            """, (user_id, password_hash.decode(), salt.decode(), answer_hash.decode()))

            conn.commit()
            return True, user_id
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()


def login_user(email: str, password: str) -> Optional[dict]:
    """
    Verify credentials using bcrypt.
    Fetches hash from user_credentials (separate table), never compares plaintext.
    Returns user dict on success or None on failure.
    """
    email = _norm_email(email)
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get user profile
            cur.execute("""
                SELECT u.user_id, u.email, u.full_name, u.phone, u.date_of_birth, u.is_active,
                       c.password_hash
                FROM users u
                JOIN user_credentials c ON c.user_id = u.user_id
                -- Soft-deleted accounts (is_active = FALSE) must not log in:
                -- consistent with the soft-delete strategy declared in schema.sql.
                WHERE u.email = %s AND u.is_active
            """, (email,))
            row = cur.fetchone()
    if not row:
        return None
    # Verify password against stored hash (never touch plaintext in DB)
    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return None
    result = dict(row)
    result.pop("password_hash")   # never return hash to caller
    parts = result["full_name"].split(" ", 1)
    result["first_name"] = parts[0]
    result["surname"] = parts[1] if len(parts) > 1 else ""
    return result


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the user's secret question, or None for an unknown email."""
    email = _norm_email(email)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT secret_question FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            return row[0] if row else None


def verify_secret_answer(email: str, answer: str) -> bool:
    """Case-insensitive check of the secret answer against its bcrypt hash.

    The answer was normalised (trim + lowercase) before hashing at
    registration, so normalising the input the same way preserves
    case-insensitive behaviour without ever storing the answer in plain text.
    """
    email = _norm_email(email)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.secret_answer_hash
                FROM user_credentials c
                JOIN users u ON u.user_id = c.user_id
                WHERE u.email = %s
            """, (email,))
            row = cur.fetchone()
    if not row or not row[0]:
        return False
    return bcrypt.checkpw(answer.strip().lower().encode(), row[0].encode())


def update_password(email: str, new_password: str) -> bool:
    """
    Update password: generate new salt, rehash, update user_credentials table only.
    The users table is never touched.
    """
    email = _norm_email(email)
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row:
                return False
            user_id = row[0]
            salt = bcrypt.gensalt()
            password_hash = bcrypt.hashpw(new_password.encode(), salt)
            cur.execute("""
                UPDATE user_credentials
                SET password_hash = %s, salt = %s, updated_at = NOW()
                WHERE user_id = %s
            """, (password_hash.decode(), salt.decode(), user_id))
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
