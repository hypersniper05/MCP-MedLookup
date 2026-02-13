# MedLookup

A medical terminology MCP server that provides instant lookups for abbreviations, conditions, drugs, and clinical definitions. Runs in Docker and connects to any MCP-compatible client.

## What It Does

**`lookup_keyword`** — Search one or more medical keywords across all sources at once:
- 4,600+ medical abbreviations and acronyms (ABG, CBC, COPD, etc.)
- NLM conditions database and ICD-10 diagnosis codes
- MedlinePlus health topic summaries
- Drug information via RxNorm (formulations, classifications) and OpenFDA (indications, dosage, warnings, interactions, mechanism of action)
- UMLS medical concepts and definitions (optional, requires free API key)

**`add_new_keyword`** — Save a missing abbreviation or term definition to the local database for future lookups.

**`remove_keyword`** — Remove a custom-added entry from the local database. Only deletes entries added via `add_new_keyword` — built-in abbreviations from the seeded database are protected.

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/hypersniper05/MCP-MedLookup.git
cd MCP-MedLookup
cp .env.example .env
```

Edit `.env` to add your UMLS API key (optional — everything else works without it):

```env
UMLS_API_KEY=your_key_here   # Free at https://uts.nlm.nih.gov/uts/profile
```

### 2. Build and run

```bash
docker compose up --build -d
```

The server starts on `http://localhost:8010/mcp`.

### 3. Connect your MCP client

Add this to your MCP client configuration (Claude Desktop, Claude Code, etc.):

```json
{
  "mcpServers": {
    "med_lookup": {
      "type": "streamable-http",
      "url": "http://localhost:8010/mcp"
    }
  }
}
```

## Usage Examples

Once connected, your AI assistant can use the tools directly:

> "What does ABG stand for?"
> → Queries `lookup_keyword` with `["ABG"]` — returns "Arterial blood gas"

> "Look up metformin and diabetes"
> → Queries `lookup_keyword` with `["metformin", "diabetes"]` — returns drug info (formulations, FDA label, drug class) and condition info (ICD-10 codes, clinical definitions)

> "Add ROSC as an abbreviation meaning Return of Spontaneous Circulation"
> → Calls `add_new_keyword` to save it locally

> "Remove the ROSC abbreviation I added"
> → Calls `remove_keyword` to delete it (only works for custom-added entries)

## Data Sources

| Source | What It Provides | API Key |
|--------|-----------------|---------|
| [imantsm/medical_abbreviations](https://github.com/imantsm/medical_abbreviations) | 4,600+ abbreviations seeded into SQLite at build time | None |
| [NLM Clinical Tables](https://clinicaltables.nlm.nih.gov) | Conditions, ICD-10 codes | None |
| [MedlinePlus](https://medlineplus.gov) | Consumer-friendly health topic summaries | None |
| [RxNorm](https://rxnav.nlm.nih.gov) | Drug formulations, classifications | None |
| [OpenFDA](https://open.fda.gov) | Full prescribing information (indications, dosage, warnings, interactions) | None |
| [UMLS](https://uts.nlm.nih.gov) | Concept definitions across medical vocabularies | Free key |

## Architecture

```
server.py              # All MCP tools and server logic (single file)
scripts/seed_db.py     # CSV-to-SQLite import (runs during Docker build)
Dockerfile
docker-compose.yml
requirements.txt
.env.example
```

The server is a single Python file using [FastMCP](https://github.com/jlowin/fastmcp) with Streamable HTTP transport. Abbreviation data is cloned from GitHub and seeded into SQLite during the Docker build. External APIs are queried at runtime with graceful degradation — if any source fails, results from other sources are still returned.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `UMLS_API_KEY` | No | — | Enables UMLS concept lookups ([get a free key](https://uts.nlm.nih.gov/uts/profile)) |
| `MCP_HOST` | No | `0.0.0.0` | Server bind address |
| `MCP_PORT` | No | `8010` | Server port |
| `DATABASE_PATH` | No | `/app/data/medical.db` | SQLite database path |

## Docker Commands

```bash
docker compose up --build -d     # Build and start
docker compose logs -f           # View logs
docker compose down              # Stop
docker compose up --build -d     # Rebuild after code changes
```

## Verifying the Server

```bash
# Check the endpoint
curl http://localhost:8010/mcp

# Interactive testing with MCP Inspector
npx @modelcontextprotocol/inspector
# Connect to: http://localhost:8010/mcp
```

## System Prompt Examples

Example system prompts for configuring your AI assistant to use these tools effectively:

- **[Detailed](system-prompt-example/system_prompt_detailed.txt)** — Full prompt with step-by-step research workflow, multi-step examples, and search rules. Best for larger models with thinking enabled.
- **[Simple](system-prompt-example/system_prompt_simple.txt)** — Compact, to-the-point prompt covering all 3 tools and core rules. Best for smaller or faster models.

## Tech Stack

- **Python 3.12** with FastMCP, httpx, Pydantic
- **SQLite** for local abbreviation and custom term storage
- **Docker** with volume-persisted database

## License

MIT
