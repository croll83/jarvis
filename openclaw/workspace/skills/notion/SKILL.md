---
name: notion
description: Notion API for creating and managing pages, databases, and blocks. Includes Jarvis workspace conventions.
homepage: https://developers.notion.com
metadata:
  {
    "openclaw":
      { "emoji": "📝", "requires": { "env": ["NOTION_API_KEY"] }, "primaryEnv": "NOTION_API_KEY" },
  }
---

# notion

Use the Notion API to create/read/update pages, data sources (databases), and blocks.

> **This skill file extends the bundled notion skill with workspace-specific conventions.**

---

## 🗂️ Jarvis Documents Index (MANDATORY)

Every new document/index created on Notion **MUST** be registered in:

- **Database:** Jarvis' Documents
- **Database ID:** `4dcac92e-a3a7-82e6-96db-0156fa587035`
- **Data Source ID:** `042ac92e-a3a7-8231-9a78-0743b6902c5a`
- **URL:** https://www.notion.so/4dcac92ea3a782e696db0156fa587035

### Creating a new entry in the index

Every time I create a new document or index page on Notion, I MUST:

1. Create the actual page/index under the right parent
2. Register it in the "Jarvis' Documents" database with ALL of these properties filled:

```bash
NOTION_KEY=$NOTION_API_KEY
DS_ID="042ac92ea3a782319a780743b6902c5a"
TODAY=$(date -u +"%Y-%m-%d")

curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"data_source_id": "'$DS_ID'"},
    "properties": {
      "Title": {"title": [{"text": {"content": "<DOCUMENT TITLE>"}}]},
      "Description": {"rich_text": [{"text": {"content": "<1-2 sentences describing the document>"}}]},
      "Group": {"select": {"name": "<GROUP>"}},
      "Tags": {"multi_select": [{"name": "<TAG1>"}, {"name": "<TAG2>"}]},
      "Status": {"select": {"name": "<STATUS>"}},
      "Created on": {"date": {"start": "'$TODAY'"}},
      "Last Edit": {"date": {"start": "'$TODAY'"}}
    },
    "children": [
      {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "mention", "mention": {"type": "page", "page": {"id": "<PAGE_ID>"}}, "plain_text": "→ Vai al documento"}
      ]}}
    ]
  }'
```

### Database Properties

| Property     | Type         | Description                          |
|--------------|--------------|--------------------------------------|
| Title        | title        | Name of the document                 |
| Description  | rich_text    | 1-2 sentence summary                 |
| Group        | select       | Category (see below)                 |
| Tags         | multi_select | Relevant topics                      |
| Status       | select       | Document lifecycle state             |
| Created on   | date         | Creation date (YYYY-MM-DD)           |
| Last Edit    | date         | Last modification date               |

### Group Options (select)

| Value       | Use for                                              |
|-------------|------------------------------------------------------|
| proposals   | Technical proposals, architecture docs, RFCs         |
| research    | Research, analysis, comparisons, deep dives          |
| ideas       | Brainstorming, rough drafts, early concepts          |
| prototyping | POCs, experiments, technical spikes                  |
| docs        | Reference docs, how-tos, guides, README              |
| analysis    | Data analysis, metrics, performance reports          |
| planning    | Roadmaps, timelines, project plans                   |

### Status Options

| Value       | Meaning                              |
|-------------|--------------------------------------|
| draft       | Work in progress, not reviewed       |
| in-progress | Being actively worked on             |
| review      | Waiting for review/feedback          |
| final       | Completed, approved                  |
| archived    | No longer active, kept for reference |

### Tag Options (multi_select)

Existing tags: TAC, TON, Polymarket, DeFi, Architecture, Revenue, Security, Frontend
Feel free to add new tags as needed — just use the name and Notion will auto-create them.

---

## Document Created Index

| Title         | Group     | Status | Notion Page ID                                |
|---------------|-----------|--------|------------------------------------------------|
| PolymarketON  | proposals | final  | 320ac92e-a3a7-808a-857f-ec14712d06dd          |

---

## API Basics

All requests need:

```bash
NOTION_KEY=$NOTION_API_KEY
curl -X GET "https://api.notion.com/v1/..." \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json"
```

> **Note:** The `Notion-Version` header is required. This skill uses `2025-09-03` (latest).

## Common Operations

**Search for pages:**
```bash
curl -X POST "https://api.notion.com/v1/search" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"query": "page title"}'
```

**Get page:**
```bash
curl "https://api.notion.com/v1/pages/{page_id}" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03"
```

**Get page content (blocks):**
```bash
curl "https://api.notion.com/v1/blocks/{page_id}/children" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03"
```

**Create page under a parent page:**
```bash
curl -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"parent": {"page_id": "<PARENT_PAGE_ID>"}, "properties": {"title": {"title": [{"text": {"content": "New Page"}}]}}}'
```

**Query a data source:**
```bash
curl -X POST "https://api.notion.com/v1/data_sources/{data_source_id}/query" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"filter": {"property": "Status", "select": {"equals": "final"}}}'
```

**Update page properties:**
```bash
curl -X PATCH "https://api.notion.com/v1/pages/{page_id}" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"properties": {"Status": {"select": {"name": "final"}}}}'
```

**Add blocks to page:**
```bash
curl -X PATCH "https://api.notion.com/v1/blocks/{page_id}/children" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "Hello"}}]}}]}'
```

## Key Differences in API 2025-09-03

- **Databases → Data Sources:** Use `/data_sources/` for queries and updates
- **Multi-datasource databases:** Use `parent: {"data_source_id": "..."}` instead of `database_id` when creating pages
- **Two IDs:** `database_id` (legacy) and `data_source_id` (new)
- **PATCH data_sources:** Use `PATCH /v1/data_sources/{id}` to add/update properties

## Notes

- Page/database IDs are UUIDs (with or without dashes)
- Rate limit: ~3 req/s, with 429 responses using `Retry-After`
- Max 100 blocks per append request, 2 levels nesting
- Payload max: 1000 block elements, 500KB
