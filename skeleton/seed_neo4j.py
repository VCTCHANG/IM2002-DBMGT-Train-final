"""
TransitFlow — Neo4j Seeder
Run once after starting Docker:
    python skeleton/seed_neo4j.py

Loads station and network data from train-mock-data/:
  - metro_stations.json         — city metro stations and adjacencies
  - national_rail_stations.json — national rail stations and adjacencies

Design your graph schema (node labels, relationship types, properties)
based on the data in these files, then implement the seed() function below.
"""

import json
import os
import sys

sys.path.insert(0, ".")

from neo4j import GraphDatabase
from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train-mock-data")
)


def _load(filename):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def seed():
    metro_stations = _load("metro_stations.json")
    rail_stations  = _load("national_rail_stations.json")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:

        session.run("MATCH (n) DETACH DELETE n")
        print("  Cleared existing graph data")

        # Per-hop fare weights (the adjacency JSON only has travel_time_min,
        # so we attach an approximate fare so Dijkstra-by-fare can run).
        METRO_FARE      = 0.30   # metro per-stop rate
        RAIL_FARE_STD   = 1.50   # national rail standard per-stop rate
        RAIL_FARE_FIRST = 2.50   # national rail first class per-stop rate
        INTERCHANGE_TIME = 5     # minutes to change between networks

        # ── Metro station nodes ──────────────────────────────────────────────
        for s in metro_stations:
            session.run(
                "MERGE (n:MetroStation {station_id: $id}) "
                "SET n.name = $name, n.lines = $lines",
                id=s["station_id"], name=s["name"], lines=s.get("lines", []),
            )
        print(f"  Created {len(metro_stations)} MetroStation nodes")

        # ── National rail station nodes ──────────────────────────────────────
        for s in rail_stations:
            session.run(
                "MERGE (n:NationalRailStation {station_id: $id}) "
                "SET n.name = $name, n.lines = $lines",
                id=s["station_id"], name=s["name"], lines=s.get("lines", []),
            )
        print(f"  Created {len(rail_stations)} NationalRailStation nodes")

        # ── Metro links ──────────────────────────────────────────────────────
        metro_links = 0
        for s in metro_stations:
            for adj in s.get("adjacent_stations", []):
                session.run(
                    "MATCH (a:MetroStation {station_id: $from_id}) "
                    "MATCH (b:MetroStation {station_id: $to_id}) "
                    "MERGE (a)-[r:METRO_LINK {line: $line}]->(b) "
                    "SET r.travel_time_min = $t, r.fare = $fare, r.fare_first = $fare",
                    from_id=s["station_id"], to_id=adj["station_id"],
                    line=adj["line"], t=adj["travel_time_min"], fare=METRO_FARE,
                )
                metro_links += 1
        print(f"  Created {metro_links} METRO_LINK edges")

        # ── National rail links ──────────────────────────────────────────────
        rail_links = 0
        for s in rail_stations:
            for adj in s.get("adjacent_stations", []):
                session.run(
                    "MATCH (a:NationalRailStation {station_id: $from_id}) "
                    "MATCH (b:NationalRailStation {station_id: $to_id}) "
                    "MERGE (a)-[r:RAIL_LINK {line: $line}]->(b) "
                    "SET r.travel_time_min = $t, r.fare = $fstd, r.fare_first = $ffirst",
                    from_id=s["station_id"], to_id=adj["station_id"], line=adj["line"],
                    t=adj["travel_time_min"], fstd=RAIL_FARE_STD, ffirst=RAIL_FARE_FIRST,
                )
                rail_links += 1
        print(f"  Created {rail_links} RAIL_LINK edges")

        # ── Interchange links (metro ↔ national rail) ────────────────────────
        interchanges = 0
        for s in metro_stations:
            nr_id = s.get("interchange_national_rail_station_id")
            if s.get("is_interchange_national_rail") and nr_id:
                session.run(
                    "MATCH (m:MetroStation {station_id: $ms}) "
                    "MATCH (r:NationalRailStation {station_id: $nr}) "
                    "MERGE (m)-[i:INTERCHANGE_TO]->(r) "
                    "SET i.travel_time_min = $t, i.fare = 0, i.fare_first = 0 "
                    "MERGE (r)-[j:INTERCHANGE_TO]->(m) "
                    "SET j.travel_time_min = $t, j.fare = 0, j.fare_first = 0",
                    ms=s["station_id"], nr=nr_id, t=INTERCHANGE_TIME,
                )
                interchanges += 1
        print(f"  Created {interchanges} INTERCHANGE_TO interchange pairs")

    driver.close()
    print("\nNeo4j graph seeded successfully.")
    print("   Open http://localhost:7475 to explore the graph.")


if __name__ == "__main__":
    print("Connecting to Neo4j...")
    seed()
