# Team 12 — TransitFlow Database Design Document

| Field | Value |
|---|---|
| Team ID | 12 |
| Members | 張子衡 (113403062) · 劉亮廷 (113403541) · 蔡博宇 (113403056) |
| Repository | <https://github.com/VCTCHANG/Team12_113403062_transitflow> |
| Date | 2026-06-11 |

This document describes the design of the three databases behind our TransitFlow
assistant: **PostgreSQL** (relational system of record), **PostgreSQL + pgvector**
(semantic policy search), and **Neo4j** (network routing). Every schema snippet and
query shown below is taken verbatim from the implementation in the repository.

---

## Section 1 — Entity-Relationship Diagram

![TransitFlow ER Diagram](https://raw.githubusercontent.com/VCTCHANG/Team12_113403062_transitflow/main/docs/TransitFlow_ER.png)

> Full-resolution versions live in the repository: [`docs/TransitFlow_ER.png`](docs/TransitFlow_ER.png)
> (raster) and [`docs/TransitFlow_ER.svg`](docs/TransitFlow_ER.svg) (vector — open in a
> browser and zoom freely). The diagram is generated programmatically from the table
> definitions, so every column name and type is exactly in sync with
> [`databases/relational/schema.sql`](databases/relational/schema.sql).

### Notation

* **Solid lines** are real database foreign-key constraints; **dashed lines** are
  relationships maintained at the application layer (polymorphic references,
  self-references, and cross-network links that deliberately have no FK — each is
  justified in Section 2 and Section 6).
* Cardinality appears **directly on every line**, twice: crow's-foot end symbols
  (`‖` = exactly 1, `|`+crow = 1..N, `○`+crow = 0..N, `|○` = 0..1) **and** a text
  label such as `1:N` or `1:1`. Minimum cardinality of 1 is a business rule enforced
  by the application (e.g. every schedule has at least one stop), since an FK alone
  cannot enforce a minimum on the parent side.
* Each entity box shows its full column list with PK / FK / UK badges and exact
  PostgreSQL types.

### Entities (16)

| Group | Entities |
|---|---|
| Metro network | `metro_stations`, `metro_schedules`, `metro_schedule_stops`, `metro_schedule_operates_on` |
| National rail network | `national_rail_stations`, `national_rail_schedules`, `national_rail_schedule_stops`, `national_rail_schedule_operates_on`, `seat_layouts` |
| Users & accounting | `users`, `user_credentials`, `payments`, `feedback` |
| Bookings & trips | `national_rail_bookings`, `metro_travels` |
| RAG (stand-alone) | `policy_documents` |

### Key relationships at a glance

| Relationship | Cardinality | Enforced by |
|---|---|---|
| `users` — `user_credentials` | 1 : 1 | FK + PK on `user_credentials.user_id` |
| `users` — `national_rail_bookings` / `metro_travels` / `feedback` | 1 : 0..N | FK `ON DELETE RESTRICT` |
| `*_schedules` — `*_schedule_stops` | 1 : 1..N | FK `ON DELETE CASCADE`; junction resolves the stations ↔ schedules M:N |
| `*_stations` — `*_schedule_stops` | 1 : 0..N | FK `ON DELETE RESTRICT` |
| `*_schedules` — `*_schedule_operates_on` | 1 : 1..N | FK `ON DELETE CASCADE` |
| `national_rail_schedules` — `seat_layouts` | 1 : 0..N | FK `ON DELETE CASCADE` |
| `*_stations` — `*_schedules` (origin / destination) | 1 : 0..N (two FKs) | FK `ON DELETE RESTRICT` |
| `national_rail_bookings` / `metro_travels` — `payments`, `feedback` | 1 : 0..N | application layer (polymorphic `booking_id`, see Section 2) |
| `metro_travels` — `metro_travels` (`day_pass_ref`) | 0..1 : 0..N | application layer (self-reference) |
| `metro_stations` — `national_rail_stations` (interchange) | 0..1 : 0..1 | application layer (cross-network, no FK to avoid a circular dependency) |

---

## Section 2 — Normalisation Justification

### 2.1 A real 3NF decision: schedule stops live in a junction table

The raw `metro_schedules.json` stores each schedule's route as an ordered array plus
a map:

```json
"stops_in_order": ["MS20", "MS05", "MS01", ...],
"travel_time_from_origin_min": {"MS20": 0, "MS05": 2, "MS01": 5, ...}
```

Our first implementation copied this shape into the database as two `JSONB` columns
on `metro_schedules`. That design violates **First Normal Form** — the column values
are not atomic, they are containers — and it hides a **functional dependency**: a
stop's position and travel time are determined by the *pair* (`schedule_id`,
`station_id`), not by `schedule_id` alone. Keeping them inside the schedule row also
made the most common query ("does this schedule serve A before B?") require
unpacking a JSON array on every call.

We therefore normalised the stop sequence into a junction table:

```sql
CREATE TABLE IF NOT EXISTS metro_schedule_stops (
    schedule_id             VARCHAR(20)  NOT NULL REFERENCES metro_schedules(schedule_id) ON DELETE CASCADE,
    station_id              VARCHAR(10)  NOT NULL REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    stop_order              INTEGER      NOT NULL,  -- 0-based position in route
    travel_time_from_origin INTEGER      NOT NULL,  -- minutes from first stop
    PRIMARY KEY (schedule_id, station_id)
);
```

This table is in **3NF**: the composite primary key (`schedule_id`, `station_id`) is
the only candidate key; `stop_order` and `travel_time_from_origin` depend on the
*whole* key (no partial dependency, so 2NF holds) and on nothing but the key (no
transitive dependency, so 3NF holds). It also resolves the many-to-many
relationship between stations and schedules that the JSON shape obscured. The
"A before B in the right direction" check becomes a plain self-join:

```sql
FROM national_rail_schedules s
JOIN national_rail_schedule_stops o ON o.schedule_id = s.schedule_id AND o.station_id = %s
JOIN national_rail_schedule_stops d ON d.schedule_id = s.schedule_id AND d.station_id = %s
WHERE o.stop_order < d.stop_order
```

The same reasoning produced `*_schedule_operates_on` (one row per operating day):
`operates_on` was a repeating group `["mon","tue",...]`, and flattening it lets the
availability query filter by day with a simple `EXISTS` instead of a JSON containment
test.

### 2.2 Deliberate de-normalisation trade-offs

We did **not** normalise everything; three choices trade redundancy for query
simplicity, and we made them consciously:

1. **Fare classes as columns, not a sub-table.** `national_rail_schedules` stores
   `std_base_fare_usd`, `std_per_stop_rate_usd`, `first_base_fare_usd`,
   `first_per_stop_rate_usd` as four columns instead of a normalised
   `fares(schedule_id, fare_class, base, rate)` table. There are exactly two fare
   classes, the business rule fixes them, and every fare lookup wants both for
   comparison — a sub-table would add a join (or a pivot) to every availability and
   fare query for zero integrity benefit.
2. **An accepted transitive dependency in `seat_layouts`.** In the source data a
   coach determines its fare class, so strictly `seat → coach → fare_class` is a
   transitive dependency and 3NF would demand a `coaches` table. We kept
   `fare_class` on the seat row because `query_available_seats` filters seats by
   fare class directly; the redundancy is written once at seed time and never
   updated, so the classic update-anomaly risk does not materialise.
3. **`lines` as JSONB on station tables.** A station's line list (e.g.
   `["M1","M2"]`) is display-only metadata. Network topology — the thing that
   actually needs integrity — lives in Neo4j and in the stops junction tables, so a
   `station_lines` junction here would be normalisation without a consumer.

One related decision is the **polymorphic `payments.booking_id`**: it can reference
either `national_rail_bookings` (`BK…`) or `metro_travels` (`MT…`). PostgreSQL
cannot declare an FK that points to "table A or table B", so we kept one payments
table (a single source of truth for money) and enforce the reference inside the
booking/cancellation transactions in the application layer. The alternatives — two
payment tables, or a supertype table — would have split financial reporting or added
a join to every payment lookup.

### 2.3 Password (and secret-answer) storage

**Algorithm.** Passwords are hashed with **bcrypt** (`bcrypt.hashpw`, cost factor 12)
at registration in `register_user()`, and verified with `bcrypt.checkpw` in
`login_user()`. Nothing password-shaped is ever stored or compared in plain text.

**Why bcrypt and not MD5 / SHA-1.** MD5 and SHA-1 are *fast* general-purpose digests
— a single GPU computes billions per second, so even salted MD5 falls to brute force
quickly, and both have known collision attacks. bcrypt is a *deliberately slow*
password-hashing function built on key stretching: its cost factor (we use 2¹²
rounds) makes each guess thousands of times more expensive, and because the cost is
stored inside the hash it can be raised in the future without breaking existing
hashes. Slowness is irrelevant for one login but crippling for an attacker who must
try millions of candidates.

**How salt is managed, and why it defeats rainbow tables.** `bcrypt.gensalt()`
generates a unique random salt per user; the salt is mixed into the hash. A rainbow
table is a precomputed `hash → password` lookup: it only works when the same
password always produces the same hash. With per-user salts, two users who both
choose `password123` store completely different values — for example
`$2b$12$N9qo8uLO…` and `$2b$12$R4fJ9aKp…` — so a precomputed table is useless and
the attacker is forced into per-user brute force at bcrypt speed. bcrypt embeds the
salt inside the hash string; we additionally store it in an explicit `salt` column
for auditability (a reviewer can confirm per-user uniqueness with one query).

**Separation from profile data.** All credential material lives in
`user_credentials`, a separate table joined 1:1 to `users`
(`user_id` PK + FK `ON DELETE CASCADE`). Queries that only need profile data
(name, email) never touch credentials — the principle of least privilege applied at
schema level. `email` remains a **candidate key** on `users` (declared `UNIQUE`) and
is normalised to lowercase before storage and lookup, because `VARCHAR` comparison
is case-sensitive.

**Secret answers are credentials too.** Late in the project we noticed
`secret_answer` sitting in plain text in `users`. Answering it resets the password,
which makes it functionally a backup password — and a plain-text backdoor around
everything above. We moved it to `user_credentials.secret_answer_hash`, bcrypt-hashed
after normalisation (`strip().lower()`), which preserves the required
case-insensitive comparison: normalise the input the same way, then `checkpw`
against the hash.

---

## Section 3 — Graph Database Design Rationale

### 3.1 What is a node, what is a relationship, what is a property — and why

**Nodes = stations** (20 `MetroStation` + 10 `NationalRailStation`). Stations are
the things a journey *visits*; routing is a traversal over them. We use two distinct
labels rather than one `Station` label with a `network` property because label-based
filtering is the cheapest predicate in Cypher (`MATCH (s:MetroStation)` touches no
properties at all) and because the two networks have genuinely different operational
rules (fares, seat booking) — the separation mirrors the domain.

**Relationships = physical links** (42 `METRO_LINK`, 18 `RAIL_LINK`, 3 bidirectional
`INTERCHANGE_TO` pairs). Each adjacency in the source JSON becomes a directed
relationship; links exist in both directions because services run both ways. The
walking connection between co-located stations of different networks is its own
relationship type, `INTERCHANGE_TO`, so cross-network paths are *opt-in*: a query
that should stay inside one network simply omits the type from its relationship
filter.

**Properties = traversal costs on the relationships.** `travel_time_min` (from the
source data), `fare` and `fare_first` (per-segment cost weights), and `line` live on
the relationship, not the node, because a cost belongs to a *segment*: the time
between A and B is a property of that link, and Dijkstra reads edge weights natively
during expansion. Interchange edges carry `travel_time_min: 5, fare: 0` — changing
networks costs time but no money. Node properties (`station_id`, `name`, `lines`)
are identity and display data.

### 3.2 Why a graph database beats SQL for these workloads

Routing is a **transitive-closure** problem: the answer is a path of *unknown
length*. In SQL this requires a recursive CTE, and a correct one must (a) carry an
accumulating array of visited stations in every row purely to prevent cycles,
(b) materialise *every* partial path because set-oriented evaluation has no concept
of "most promising path first", and (c) re-join the edge table at every recursion
depth. There is no goal-direction and no early termination — the database cannot
know it has found the best route to NR05 until it has expanded everything shorter.

Neo4j stores adjacency *in the record* (index-free adjacency): expanding a node's
neighbours is pointer-chasing, not an index lookup or join. On top of that,
`apoc.algo.dijkstra` runs the real Dijkstra algorithm — a priority queue ordered by
accumulated weight, `O((V+E) log V)`, terminating as soon as the target is settled —
and `shortestPath()` gives goal-directed BFS for hop-count questions. Our network is
small (30 nodes / 66 directed edges), but the asymmetry is structural: every extra
hop costs SQL another self-join over the whole edge set, while the graph only ever
touches the frontier it is expanding.

### 3.3 Two query types the model makes natural

**(1) Weighted fastest route — Dijkstra over `travel_time_min`**
(`query_shortest_route` in `databases/graph/queries.py`):

```cypher
MATCH (a {station_id: $o}), (b {station_id: $d})
CALL apoc.algo.dijkstra(a, b, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', 'travel_time_min')
YIELD path, weight
RETURN path, weight ORDER BY weight LIMIT 1
```

The relationship filter *is* the network policy: pass `'METRO_LINK'` for
metro-only, `'RAIL_LINK'` for rail-only, or the union to allow everything. The same
function answers `query_cheapest_route` by swapping the weight property to `fare`
or `fare_first` — which is why costs live on relationships. Expressing "swap the
optimisation metric" in SQL would mean rewriting the recursive CTE.

**(2) Cross-network interchange path** (`query_interchange_path`): because
interchanges are a distinct relationship type, a metro→rail journey is the *same*
Dijkstra call with `INTERCHANGE_TO` included in the filter; afterwards we read
`type(r)` off each path segment to report exactly where the passenger changes
networks. The path crosses the boundary only if it is worth 5 minutes — the model
prices the decision instead of special-casing it.

**(3) Delay ripple** (`query_delay_ripple`): "which stations are within N hops of a
disruption" is a variable-length pattern with a path function:

```cypher
MATCH (s {station_id: $id})
MATCH (n)
MATCH sp = shortestPath((s)-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*0..2]-(n))
RETURN n.station_id, n.name, length(sp) AS hops_away
```

`length(sp)` *is* the `hops_away` answer; the `*0..N` lower bound of 0 makes
`hops = 0` correctly return only the delayed station itself. The SQL equivalent is
another recursive CTE with manual depth tracking.

### 3.4 Node identity

Nodes are identified by the **`station_id`** property (`MS01`–`MS20`,
`NR01`–`NR10`) — the same operator-assigned natural key as the relational PK, which
keeps cross-database joins trivial (the agent passes IDs between PostgreSQL results
and Cypher parameters unchanged). It is guaranteed unique per network, stable, and
human-readable. `name` would be a poor identity: nothing guarantees uniqueness and
display names can change. All seeding uses `MERGE (n:MetroStation {station_id: $id})`,
so the seeder is idempotent — re-running updates properties instead of duplicating
nodes, and relationship `MERGE`s are anchored to the same identities.

---

## Section 4 — Vector / RAG Design

### 4.1 What is embedded, and why cosine similarity

We embed the **policy knowledge base**: 17 documents loaded from
`refund_policy.json`, `ticket_types.json`, `booking_rules.json` and
`travel_policies.json` (refund windows, ticket rules, luggage/bike/pet policies…).
Each entry becomes one row in `policy_documents` with its prose content and a
768-dimensional embedding produced by the same model that later embeds user
questions.

Similarity is measured with **cosine similarity**, and the choice matters: cosine
compares the **angle** between two vectors, ignoring their **magnitude**. Embedding
norms vary with surface features — longer, denser documents tend to produce
larger-magnitude vectors — while *meaning* is encoded in the vector's direction in
the embedding space. A dot-product or Euclidean ranking would let a long, verbose
policy outrank a short, exactly-on-topic one purely because of vector length;
cosine normalises that away, so "can I get money back for a delay?" ranks the
delay-compensation policy first even though it shares almost no keywords with the
document text. The index is built for exactly this metric:

```sql
CREATE INDEX IF NOT EXISTS policy_documents_embedding_idx
    ON policy_documents USING hnsw (embedding vector_cosine_ops);
```

an HNSW approximate-nearest-neighbour graph, so retrieval stays sub-linear as the
knowledge base grows.

### 4.2 The full RAG pipeline

1. **Query embedding.** The user's question is embedded by `llm.embed(question)` —
   Ollama `nomic-embed-text`, returning a 768-dim vector. Critically this is the
   *same model* used at seed time; query and documents must live in the same
   embedding space for distances to mean anything.
2. **Similarity search.** `query_policy_vector_search()` runs, with `<=>` being
   pgvector's cosine-distance operator (similarity = 1 − distance):

   ```sql
   SELECT title, category, content,
          1 - (embedding <=> %s::vector) AS similarity
   FROM policy_documents
   WHERE 1 - (embedding <=> %s::vector) > 0.5      -- relevance floor
   ORDER BY embedding <=> %s::vector
   LIMIT 3;                                        -- top-k
   ```

   The 0.5 threshold stops irrelevant documents from being padded into the context
   when nothing in the knowledge base actually matches.
3. **Retrieved documents → prompt.** The top-3 rows are flattened by the agent's
   normaliser into a plain-text block (title, category, content, similarity score)
   and injected into the LLM prompt alongside the original question.
4. **Answer generation.** The LLM answers *from the retrieved text* — e.g. it quotes
   refund window RF005's percentages rather than inventing them. Retrieval grounds
   the generation; the model supplies wording, not facts.

### 4.3 Embedding dimension, and what happens if the provider is switched

Our implementation uses **768 dimensions** — the output size of Ollama's
`nomic-embed-text` — and the column is declared `embedding vector(768)`. Gemini's
`gemini-embedding-001` produces **3072-dimensional** vectors instead.

The dimension is baked into the schema *and* into every stored vector, so switching
providers after seeding breaks retrieval in two layers. First, pgvector refuses to
compare vectors of different lengths — a 3072-dim query against 768-dim rows fails
with a dimension-mismatch error, so every policy question errors out. Second, even
if the column were resized, old vectors would be meaningless: the two models define
*different embedding spaces*, and a coordinate-wise comparison between them is
noise, not similarity. The only correct recovery is: change the schema to
`vector(3072)`, wipe the database (`docker compose down -v && docker compose up -d`),
and re-embed everything with the new provider (`python skeleton/seed_vectors.py`).
For exactly this reason our team rule was to agree on one provider (Ollama) in
`.env` **before** anyone seeded, since vectors written by different teammates with
different providers would be silently incompatible.

---

## Section 5 — AI Tool Usage Evidence

We used AI assistants (Claude, and ChatGPT for quick checks) throughout the project.
Four representative examples, including one where the AI's output was wrong and had
to be corrected.

### Example 1 — Schema design: the AI's JSONB recommendation was wrong for us

* **Context:** Designing tables for `metro_schedules.json`, where each schedule
  carries an ordered `stops_in_order` array and a `travel_time_from_origin_min` map.
* **Prompt:** *"Design PostgreSQL tables for this schedule JSON (sample attached).
  Each schedule has an ordered list of stops and a travel-time map keyed by station.
  What's the best way to store the stops?"*
* **Outcome:** The AI confidently recommended keeping both fields as `JSONB` columns
  and querying with `jsonb_array_elements_text()`, arguing relational tables handle
  ordered arrays poorly. We adopted it — and it worked — but while writing this
  document's normalisation section we realised the design violates 1NF (non-atomic
  values) and buries the functional dependency (`schedule_id`, `station_id`) →
  (`stop_order`, `travel_time`). When we challenged the AI with *"critique your own
  JSONB design against 3NF"* it reversed its position and produced the junction-table
  design we now use (`*_schedule_stops`, `*_schedule_operates_on`). The git history
  records both states (commit `48348f2` JSONB → commit `69b55bf` junction tables).
  **Lesson:** the AI optimised for implementation convenience, not normal forms,
  until normalisation was explicitly made part of the question.

### Example 2 — Query writing: direction-aware availability

* **Context:** Implementing `query_national_rail_availability`, which must return
  only schedules serving the origin *before* the destination, with live seat counts.
* **Prompt:** *"Given tables national_rail_schedules and national_rail_schedule_stops
  (schedule_id, station_id, stop_order), write a query returning schedules that stop
  at both :origin and :dest in that order, plus a seat-availability count from
  seat_layouts minus non-cancelled bookings on :date."*
* **Outcome:** The AI produced the double-join pattern we kept (join the stops table
  twice, `WHERE o.stop_order < d.stop_order`) — cleaner than our draft, which had
  fetched the stop list and compared positions in Python. During final testing we
  found a gap *neither* we nor the AI had covered: schedules NR_SCH05–08 run
  weekdays only, but a Saturday query still returned them. We added an `EXISTS`
  filter against `national_rail_schedule_operates_on` using
  `to_char(%s::date, 'dy')`. The AI accelerated the correct join shape; the
  domain edge case still came from us reading the data.

### Example 3 — Debugging: the email case-sensitivity bug

* **Context:** Pre-submission audit. We asked an AI to review the whole
  register → login → book → cancel flow for bugs a grader could trigger.
* **Prompt:** *"Here are ui.py and queries.py. Trace the registration and login data
  flow and list any input that breaks a later lookup. Pay attention to what is
  stored vs what is compared."*
* **Outcome:** It found a real bug we had never hit: `do_register` lower-cased the
  email kept in session state, but `register_user` stored the raw-case email in the
  database. Any user registering as `Alice@Example.com` could log in but then
  `query_user_bookings` / `make_booking` found no profile, because `VARCHAR`
  comparison is case-sensitive. We never noticed it because we always typed
  lowercase emails in tests. Fix: a single `_norm_email()` helper
  (`strip().lower()`) applied at storage *and* every lookup, then a regression test
  registering with mixed case and logging in with a different case.

### Example 4 — Design rationale: treating the secret answer as a credential

* **Context:** The course requires that passwords are never stored in plain text and
  never in the user table. Our `users` table still contained plain-text
  `secret_answer`.
* **Prompt:** *"Our users table has secret_question and secret_answer columns; the
  answer lets a user reset their password. Is storing these here a security problem,
  and how do we fix it without breaking the required case-insensitive answer check?"*
* **Outcome:** The AI's analysis: the secret *question* is public by design (it is
  shown to anyone entering an email) and may stay; the secret *answer* is
  functionally a backup password — stored in plain text it is an account-takeover
  backdoor that bypasses bcrypt entirely. The fix it proposed, which we implemented:
  move it to `user_credentials.secret_answer_hash`, normalise (`strip().lower()`)
  **before** hashing, and normalise the user's input the same way before
  `bcrypt.checkpw` — preserving case-insensitive matching without ever storing the
  answer. We migrated schema, seeder and queries, and verified the full
  forgot-password flow afterwards.

---

## Section 6 — Reflection & Trade-offs

### Two design decisions and their reasoning

**1. Natural `VARCHAR` keys instead of `SERIAL` or `UUID`.** Every core entity keys
on the operator-assigned code from the source data (`MS01`, `NR_SCH01`, `RU01`,
`BK001`). We chose this over `SERIAL` because the codes already exist and are
guaranteed unique by the operator — adding an auto-increment surrogate would force
every seeder and every cross-database call to maintain a translation map, and the
graph database would still need the natural code anyway (Neo4j node identity,
Section 3.4). We chose it over `UUID` because we are a single-region, single-writer
system that gains nothing from distributed ID generation, while losing log and
receipt readability (`BK-A1B2C3` in a confirmation message beats a 36-character
UUID). The accepted trade-off: `VARCHAR` joins are marginally slower than `INTEGER`
joins, irrelevant at our row counts.

**2. Soft delete everywhere, paired with `ON DELETE RESTRICT`.** Users are
deactivated (`is_active = FALSE`), bookings are cancelled (`status = 'cancelled'`)
— nothing financial is ever physically deleted. A hard `DELETE ... CASCADE` from
`users` would destroy bookings and orphan payments, i.e. destroy the audit trail of
money we have taken. The schema enforces the policy: FKs from bookings to users and
stations are `RESTRICT` (you cannot delete a parent that has financial children),
while genuinely dependent data (credentials, a schedule's stops) cascades. The cost
is that every "live" query must filter (`status != 'cancelled'`, `is_active`), and we
pay it consciously — e.g. `login_user` rejects deactivated accounts explicitly.

### What would change in production

**Schema migrations.** Our workflow for any schema change is
`docker compose down -v && docker compose up -d` plus full re-seeding — we throw the
database away. That is fine when all data is regenerable mock data and unthinkable
in production, where the data *is* the product. A real deployment would use a
migration tool (Alembic, Flyway): each change becomes a versioned, ordered,
reviewed migration script (`003_add_secret_answer_hash.sql`) applied incrementally
without data loss, with a tested rollback path. Our secret-answer fix is a concrete
example: in this project it was "edit `schema.sql`, wipe, re-seed"; in production it
would have to be an online migration — add the nullable column, backfill hashes in
batches, deploy code reading the new column, then drop `users.secret_answer` — each
step reversible. Secondarily, our `_connect()` opens a fresh psycopg2 connection per
query, which is correct but wasteful; production would put PgBouncer or an
application-side pool in front of PostgreSQL, and credentials would come from a
secret manager instead of a `.env` file.
