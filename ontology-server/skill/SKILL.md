---
name: ontology-remote
version: 0.1.0
description: "REST API-based knowledge graph for structured agent memory, context filtering (ACL), and autonomous execution. Use when creating/querying entities (Person, Project, Task, Transaction, Strategy), linking related objects, enforcing constraints, or planning multi-step actions. Trigger on 'remember', 'what do I know about', 'link X to Y', 'show dependencies', entity CRUD, financial execution logging, or when checking workload/blocked tasks."
user-invocable: true
metadata:
  openclaw:
    emoji: "🧠"
    requires:
      env: ["ONTOLOGY_URL"]
      bins: ["curl"]
---

# Agentic Ontology API (v2.2)

A typed vocabulary, constraint system, and execution engine representing knowledge as a verifiable graph, accessible via REST API.

## Core Concept

Everything is an **entity** with a **type**, **properties**, and **relations** to other entities. Every mutation is validated against type constraints before committing. Data is stored in SQLite for high-performance querying and graph traversal.

**Crucial ACL Rule:** All interactions MUST include the `X-Speaker-Id` header. The graph automatically filters entities based on the speaker's privilege level and each entity's `visibility` property. Missing the header means anonymous access (public data only, no writes).

```json
// Entity
{ "id": "task_abc", "type": "Task", "properties": {"title": "Buy BTC", "status_enum": "open", "owner": "user_marco"}, "created": "...", "updated": "..." }

// Relation
{ "from_id": "task_abc", "rel_type": "assigned_to", "to_id": "agent_01", "properties": {} }
```

## When to Use

| Trigger | Action |
| --- | --- |
| "Remember that..." | `POST /entities` or `PATCH /entities/{id}` |
| "What do I know about X?" | `POST /entities/query` or `GET /entities/{id}` |
| "Link X to Y" | `POST /relations` |
| "Show all tasks for project Z" | `GET /entities/{id}/related` (dir: incoming, rel: part_of) |
| "Why is this project stuck?" | `GET /helpers/blocked-tasks` |
| "Who has too much work?" | `GET /helpers/workload` |
| "Execute a trade/action" | Call external API, then `POST /entities` (Type: Transaction) |
| "List all my accounts" | `GET /entities?type=Account` |
| "Remove the link between X and Y" | `DELETE /relations?from_id=X&rel_type=R&to_id=Y` |
| "Import these entities" | `POST /entities/bulk` |

## Entity Types (v2.2)

See `references/schema.md` for full definitions. Key domains:

* **Agents & People:** `Person` (role, DoB, timezone), `Organization` (description)
* **Cognitive:** `Topic` (semantic grouping), `Preference` (user settings)
* **Work:** `Project`, `Task`, `Goal`, `Strategy` (automated execution rules)
* **Financial & On-Chain:** `Transaction` (immutable record), `Account` (wallets, banks, authorizations), `Skill` (binary, skill_md)
* **Time & Place:** `Event`, `Location` (description)
* **Information:** `Document`, `Message`, `Thread`, `Note`
* **Resources:** `Device` (description), `Credential` (secret_ref only, no plaintext secrets)
* **Meta:** `Action`, `Policy`

### Key Relations

* **`related_to`** (Person↔Person): Family/social/professional — `relation_type`: spouse, child, parent, grandparent, grandchild, sibling, friend, colleague, acquaintance
* **`owns`**: Person/Org → Account, Device, Document, Project, Location, Organization
* **`has_skill`**: Location/Person/Org → Skill (capabilities available at a location)
* **`located_at`**: Event/Person/Device → Location

## API Reference

Base URL: `$ONTOLOGY_URL` (default: `http://127.0.0.1:8100`)

**Required headers on every request:**
```
X-Speaker-Id: <speaker_id>
Authorization: Bearer $ONTOLOGY_API_TOKEN    # if token configured
Content-Type: application/json               # for POST/PATCH
```

### Entity CRUD

#### Create entity
```bash
curl -s -X POST "$ONTOLOGY_URL/entities" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "Person", "properties": {"name": "Alice", "visibility": "public"}}'
```
Owner is auto-assigned from `X-Speaker-Id` if not specified.

#### Get entity by ID
```bash
curl -s "$ONTOLOGY_URL/entities/{id}" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"
```

#### List entities (paginated)
```bash
curl -s "$ONTOLOGY_URL/entities?type=Task&limit=50&offset=0" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"
```

#### Search by properties
```bash
curl -s -X POST "$ONTOLOGY_URL/entities/query?type=Task&limit=100" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status_enum": "open"}'
```

#### Update entity
```bash
curl -s -X PATCH "$ONTOLOGY_URL/entities/{id}" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"properties": {"status_enum": "done"}}'
```
**Note:** Confirmed `Transaction` entities are immutable and cannot be updated.

#### Delete entity
```bash
curl -s -X DELETE "$ONTOLOGY_URL/entities/{id}" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"
```
Deleting an entity cascades to all its relations.

#### Bulk create
```bash
curl -s -X POST "$ONTOLOGY_URL/entities/bulk" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '[{"type": "Person", "properties": {"name": "Alice"}}, {"type": "Person", "properties": {"name": "Bob"}}]'
```

### Relations (Graph Edges)

#### Create relation
```bash
curl -s -X POST "$ONTOLOGY_URL/relations" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"from_id": "proj_001", "rel_type": "has_task", "to_id": "task_001"}'
```

#### Get related entities
```bash
# Outgoing: what does this entity point to?
curl -s "$ONTOLOGY_URL/entities/{id}/related?rel=has_task&dir=outgoing" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Incoming: what points to this entity?
curl -s "$ONTOLOGY_URL/entities/{id}/related?rel=part_of&dir=incoming" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Both directions (all neighbors)
curl -s "$ONTOLOGY_URL/entities/{id}/related?dir=both" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"
```

#### Delete relation
```bash
curl -s -X DELETE "$ONTOLOGY_URL/relations?from_id=proj_001&rel_type=has_task&to_id=task_001" \
  -H "X-Speaker-Id: $SPEAKER_ID" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"
```

### Helpers (Advanced Queries)

```bash
# Tasks blocked by incomplete dependencies
curl -s "$ONTOLOGY_URL/helpers/blocked-tasks" -H "X-Speaker-Id: $SPEAKER_ID" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Workload: open task count per person
curl -s "$ONTOLOGY_URL/helpers/workload" -H "X-Speaker-Id: $SPEAKER_ID" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Project summary: task count by status
curl -s "$ONTOLOGY_URL/helpers/project-summary/{project_id}" -H "X-Speaker-Id: $SPEAKER_ID" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Shortest path between two entities
curl -s "$ONTOLOGY_URL/helpers/path?from_id=X&to_id=Y&depth=5" -H "X-Speaker-Id: $SPEAKER_ID" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"
```

### Admin

```bash
# Validate graph against schema
curl -s -X POST "$ONTOLOGY_URL/admin/validate" -H "Authorization: Bearer $ONTOLOGY_API_TOKEN"

# Health check
curl -s "$ONTOLOGY_URL/health"

# OpenAPI spec (for dynamic tool calling)
curl -s "$ONTOLOGY_URL/openapi.json"
```

## Constraints & Validation

Defined in `schema.yaml`. The API validates on every write.

* **Enums:** Always use the `_enum` suffix for enum-constrained properties (e.g., `status_enum: "open"`).
* **Refs:** Link entities using the string ID (e.g., `"assignee": "pers_abc123"`).
* **Required:** Missing required properties on create → HTTP 422.
* **Forbidden:** Properties like `password`, `secret`, `token` on Credential → HTTP 422.
* **Immutability:** A `Transaction` with `status_enum: "confirmed"` cannot be modified → HTTP 409.
* **Acyclicity:** The `blocks` and `depends_on` relations prevent circular dependencies.

## Autonomous Execution Pattern (Financial/Crypto)

When instructed to perform an automated action (e.g., Trading):

1. **Read Intent:** Find the relevant `Task` or `Strategy`.
2. **Locate Resources:** Find the `Account` and its associated `Skill` (e.g., `debank_api`).
3. **Execute:** Perform the action via the external API.
4. **Log Immutably:** Create a `Transaction` entity:
   ```json
   {"type": "Transaction", "properties": {"type_enum": "buy", "account": "acc_binance", "asset_in": "USDT", "amount_in": 100, "asset_out": "BTC", "amount_out": 0.0011, "status_enum": "confirmed", "executor": "agent_self", "timestamp": "2026-02-17T10:30:00Z"}}
   ```
5. **Link Intent:** `POST /relations` → `from: tx_001`, `rel: originated_from`, `to: task_123`.

## Planning as Graph Transformation

Model multi-step plans as a sequence of API calls:

```text
Plan: "Delegate project setup to Agent B"

1. POST /entities → Create Task { "title": "Setup DB", "status_enum": "open" }
2. POST /relations → RELATE Project → has_task → Task
3. POST /relations → RELATE User → delegates_to → Agent_B
4. PATCH /entities/{task_id} → Set { "assignee": "Agent_B" }
```

If an API call returns a 400/422/500 error (constraint violation), abort the sequence and report the failure.

## ACL & Speaker Identity

### Speaker-Id Rules

The `X-Speaker-Id` header identifies **who** is making the request. You MUST always set it correctly:

| Context | Speaker-Id | Why |
|---|---|---|
| Telegram session for Marco | `user_marco` | Human user, standard ACL applies |
| Telegram session for Ada | `user_ada` | Human user, standard ACL applies |
| Voice from microphone (AtomS3R) | `user_marco` (or detected speaker) | Matched to the person speaking |
| Cron job, heartbeat, automated agent task | `jarvis-agent` | Agent identity → full read/write access |
| Other service agents | Their registered Person ID | Agent identity → full read/write access |

**Important:** When OpenClaw handles a Telegram session, it MUST present itself as the human user (e.g., `user_marco`), NOT as `jarvis-agent`. The agent identity is reserved for autonomous/background operations where no specific human initiated the request.

### Privilege Tiers

Access is determined by the Person entity matching the speaker-id:

| Tier | Condition | Read | Write |
|---|---|---|---|
| **Admin** | Person with `role_enum: "admin"` | Everything | Everything |
| **Agent** | Person with `type_enum: "Agent"` | Everything | Everything |
| **User** | Authenticated speaker (has Person entity) | Public + family + own | Own only |
| **Anonymous** | Missing/unknown `X-Speaker-Id` | Public only | Denied (403) |

### Visibility per Entity

Every entity type supports `visibility` (defaults to `public` if omitted):

| Visibility | Who can see it |
|---|---|
| `public` (default) | Everyone, including anonymous |
| `family` | Any authenticated speaker (non-anonymous) |
| `private` | Only the owner (matched via `X-Speaker-Id`) |

Set visibility on entity creation:
```json
{"type": "Account", "properties": {"service": "binance", "username": "marco", "visibility": "private"}}
```

### Bootstrapping Identities

Before ACL works, create Person entities for all speakers:
```bash
# Human admin
curl -s -X POST "$ONTOLOGY_URL/entities" \
  -H "X-Speaker-Id: user_marco" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "Person", "properties": {"name": "Marco", "role_enum": "admin", "type_enum": "Human", "visibility": "public"}}'

# Agent (full access for background tasks)
curl -s -X POST "$ONTOLOGY_URL/entities" \
  -H "X-Speaker-Id: jarvis-agent" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "Person", "properties": {"name": "Jarvis Agent", "type_enum": "Agent", "visibility": "public"}}'

# Family member (standard user)
curl -s -X POST "$ONTOLOGY_URL/entities" \
  -H "X-Speaker-Id: user_ada" \
  -H "Authorization: Bearer $ONTOLOGY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "Person", "properties": {"name": "Ada", "role_enum": "user", "type_enum": "Human", "visibility": "family"}}'
```

## References

- `references/schema.md` — Full type definitions and constraint patterns
- `references/queries.md` — Query language and traversal examples

## Instruction Scope

Runtime instructions MUST operate via the REST API (`$ONTOLOGY_URL`). Direct file manipulation of the SQLite DB is forbidden to prevent locking issues. You MUST always include the `X-Speaker-Id` header set to the correct identity for the current context (see Speaker-Id Rules above). When acting on behalf of a human user (e.g., Telegram session), use their speaker-id, not the agent's. Do not invent relations or entity types not present in `schema.yaml`.
