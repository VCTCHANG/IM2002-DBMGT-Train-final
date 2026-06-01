"""
TransitFlow — Neo4j Graph Database Layer
=========================================
This module handles all queries to Neo4j.

GRAPH ROLE:
  - Model the dual transit network (city metro M1–M4 + national rail NR1–NR2)
  - Find fastest routes (Dijkstra by travel_time_min via APOC)
  - Find cheapest routes (Dijkstra by fare via APOC)
  - Find alternative routes avoiding a given station
  - Find cross-network interchange paths (metro → rail or rail → metro)
  - Show delay ripple: which stations are affected within N hops

STUDENT TASK
------------
Design your graph schema (node labels, relationship types, properties)
based on the data in train-mock-data/, seed it with skeleton/seed_neo4j.py,
then implement the query_ functions below.

Functions prefixed with `query_` are called by the agent (skeleton/agent.py).
"""

from __future__ import annotations

from typing import Optional

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


def _driver():
    """Return a Neo4j driver. Caller is responsible for closing."""
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a session, run Cypher, return data.

def example_count_nodes() -> int:
    """Example: count all nodes currently in the graph."""
    with _driver() as driver:
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS total")
            return result.single()["total"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _rel_filter(network: str) -> str:
    """APOC dijkstra relationship filter (no direction arrow = both ways)."""
    if network == "metro":
        return "METRO_LINK"
    if network == "rail":
        return "RAIL_LINK"
    return "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"


def _path_stations(path) -> list[dict]:
    """Convert a Neo4j path to an ordered list of station dicts."""
    return [{"station_id": n["station_id"], "name": n["name"]} for n in path.nodes]


def _path_legs(path) -> list[dict]:
    """Convert a Neo4j path to a list of per-hop leg dicts."""
    nodes = list(path.nodes)
    legs = []
    for i, rel in enumerate(path.relationships):
        a, b = nodes[i], nodes[i + 1]
        legs.append({
            "from_id":         a["station_id"],
            "from_name":       a["name"],
            "to_id":           b["station_id"],
            "to_name":         b["name"],
            "link_type":       rel.type,
            "line":            rel.get("line"),
            "travel_time_min": rel.get("travel_time_min"),
        })
    return legs


# ── FASTEST ROUTE (Dijkstra by travel_time_min) ───────────────────────────────

def query_shortest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
) -> dict:
    """
    Find the fastest path between two stations, minimising total travel time.
    Uses apoc.algo.dijkstra (APOC required; enabled in docker-compose.yml).

    Args:
        origin_id:       e.g. "MS01" or "NR01"
        destination_id:  e.g. "MS09" or "NR05"
        network:         "metro", "rail", or "auto" (inferred from IDs)

    Returns:
        dict with keys: found, origin_id, destination_id,
                        total_time_min, path (list of station dicts), legs
    """
    cypher = (
        "MATCH (a {station_id: $o}), (b {station_id: $d}) "
        "CALL apoc.algo.dijkstra(a, b, $rels, 'travel_time_min') "
        "YIELD path, weight "
        "RETURN path, weight ORDER BY weight LIMIT 1"
    )
    with _driver() as driver, driver.session() as session:
        rec = session.run(cypher, o=origin_id, d=destination_id,
                          rels=_rel_filter(network)).single()
        if rec is None:
            return {"found": False, "origin_id": origin_id,
                    "destination_id": destination_id,
                    "total_time_min": None, "path": [], "legs": []}
        return {
            "found": True,
            "origin_id": origin_id,
            "destination_id": destination_id,
            "total_time_min": rec["weight"],
            "path": _path_stations(rec["path"]),
            "legs": _path_legs(rec["path"]),
        }


# ── CHEAPEST ROUTE (Dijkstra by fare) ────────────────────────────────────────

def query_cheapest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
    fare_class: str = "standard",
) -> dict:
    """
    Find the cheapest path between two stations, minimising total estimated fare.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        network:         "metro", "rail", or "auto"
        fare_class:      "standard" or "first" (national rail only)

    Returns:
        dict with found, total_fare_usd (approximate), stations, legs
    """
    weight_prop = "fare_first" if fare_class == "first" else "fare"
    cypher = (
        "MATCH (a {station_id: $o}), (b {station_id: $d}) "
        f"CALL apoc.algo.dijkstra(a, b, $rels, '{weight_prop}') "
        "YIELD path, weight "
        "RETURN path, weight ORDER BY weight LIMIT 1"
    )
    with _driver() as driver, driver.session() as session:
        rec = session.run(cypher, o=origin_id, d=destination_id,
                          rels=_rel_filter(network)).single()
        if rec is None:
            return {"found": False, "origin_id": origin_id,
                    "destination_id": destination_id,
                    "total_fare_usd": None, "fare_class": fare_class,
                    "stations": [], "legs": []}
        return {
            "found": True,
            "origin_id": origin_id,
            "destination_id": destination_id,
            "fare_class": fare_class,
            "total_fare_usd": round(rec["weight"], 2),
            "stations": _path_stations(rec["path"]),
            "legs": _path_legs(rec["path"]),
        }


# ── ALTERNATIVE ROUTES (avoiding a station) ───────────────────────────────────

def query_alternative_routes(
    origin_id: str,
    destination_id: str,
    avoid_station_id: str,
    network: str = "auto",
    max_routes: int = 3,
) -> list[list[dict]]:
    """
    Find paths between two stations that avoid a specific intermediate station.
    Useful for routing around a delayed or closed station.

    Args:
        origin_id:         e.g. "NR01"
        destination_id:    e.g. "NR05"
        avoid_station_id:  e.g. "NR03"
        network:           "metro", "rail", or "auto"
        max_routes:        max number of alternatives to return

    Returns:
        List of routes, each route is a list of leg dicts
    """
    # apoc.path.expandConfig prunes during traversal (NODE_PATH = simple paths,
    # blacklistNodes skips the avoided station), so it won't blow up like a raw
    # variable-length pattern would.
    cypher = (
        "MATCH (a {station_id: $o}), (b {station_id: $d}), (avoid {station_id: $avoid}) "
        "CALL apoc.path.expandConfig(a, { "
        "  relationshipFilter: $relf, "
        "  blacklistNodes: [avoid], "
        "  terminatorNodes: [b], "
        "  uniqueness: 'NODE_PATH', "
        "  minLevel: 1, maxLevel: 12 "
        "}) YIELD path "
        "WITH path, reduce(t = 0, r IN relationships(path) | t + r.travel_time_min) AS total "
        "RETURN path, total ORDER BY total ASC LIMIT $max"
    )
    with _driver() as driver, driver.session() as session:
        # Pull extra rows then dedupe by station sequence: parallel line-edges
        # between the same stops would otherwise yield identical node paths.
        result = session.run(cypher, o=origin_id, d=destination_id,
                             avoid=avoid_station_id, relf=_rel_filter(network),
                             max=int(max_routes) * 8)
        routes, seen = [], set()
        for rec in result:
            legs = _path_legs(rec["path"])
            key = tuple([legs[0]["from_id"]] + [leg["to_id"] for leg in legs])
            if key in seen:
                continue
            seen.add(key)
            routes.append(legs)
            if len(routes) >= int(max_routes):
                break
        return routes


# ── CROSS-NETWORK INTERCHANGE PATH ───────────────────────────────────────────

def query_interchange_path(origin_id: str, destination_id: str) -> dict:
    """
    Find a path between a metro station and a national rail station (or vice versa)
    crossing the network boundary via interchange relationships.

    Args:
        origin_id:       e.g. "MS03" (metro) or "NR05" (national rail)
        destination_id:  e.g. "NR05" (national rail) or "MS09" (metro)

    Returns:
        dict with found, stations list, interchange points, total_time_min
    """
    cypher = (
        "MATCH (a {station_id: $o}), (b {station_id: $d}) "
        "CALL apoc.algo.dijkstra(a, b, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', "
        "'travel_time_min') "
        "YIELD path, weight "
        "RETURN path, weight ORDER BY weight LIMIT 1"
    )
    with _driver() as driver, driver.session() as session:
        rec = session.run(cypher, o=origin_id, d=destination_id).single()
        if rec is None:
            return {"found": False, "origin_id": origin_id,
                    "destination_id": destination_id,
                    "total_time_min": None, "stations": [],
                    "interchange_points": [], "legs": []}
        legs = _path_legs(rec["path"])
        interchange_points = [
            {"from_id": leg["from_id"], "to_id": leg["to_id"]}
            for leg in legs if leg["link_type"] == "INTERCHANGE_TO"
        ]
        return {
            "found": True,
            "origin_id": origin_id,
            "destination_id": destination_id,
            "total_time_min": rec["weight"],
            "stations": _path_stations(rec["path"]),
            "interchange_points": interchange_points,
            "legs": legs,
        }


# ── DELAY RIPPLE ANALYSIS ─────────────────────────────────────────────────────

def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]:
    """
    Find all stations within N hops of a delayed or disrupted station.
    Works on both metro and national rail networks.

    Args:
        delayed_station_id: e.g. "NR03" or "MS01"
        hops:               how many connections out to search (default 2)

    Returns:
        List of dicts: {station_id, name, hops_away, lines_affected}
    """
    hops = int(hops)
    cypher = (
        "MATCH (s {station_id: $id}) "
        "MATCH (n) WHERE n.station_id <> $id "
        f"MATCH sp = shortestPath((s)-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..{hops}]-(n)) "
        "RETURN DISTINCT n.station_id AS station_id, n.name AS name, "
        "length(sp) AS hops_away, n.lines AS lines_affected "
        "ORDER BY hops_away, station_id"
    )
    with _driver() as driver, driver.session() as session:
        result = session.run(cypher, id=delayed_station_id)
        return [dict(rec) for rec in result]


# ── STATION CONNECTIONS ───────────────────────────────────────────────────────

def query_station_connections(station_id: str) -> list[dict]:
    """
    List all direct connections from a given station.

    Args:
        station_id: e.g. "MS01" or "NR01"
    """
    cypher = (
        "MATCH (s {station_id: $id})-[r]-(n) "
        "RETURN DISTINCT n.station_id AS station_id, n.name AS name, "
        "type(r) AS link_type, r.line AS line, r.travel_time_min AS travel_time_min "
        "ORDER BY link_type, station_id"
    )
    with _driver() as driver, driver.session() as session:
        result = session.run(cypher, id=station_id)
        return [dict(rec) for rec in result]
