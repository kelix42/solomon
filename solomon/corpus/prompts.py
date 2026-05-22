"""Prompt templates for the corpus Karpathy LLM-Wiki passes.

Ported from /root/projects/solomon-from-drive/corpus_ingest/prompts.py with
no semantic changes — wiki conventions (entity / concept / playbook
section structure) match REPORT-CORPUS.md §1.3.

Two prompts:
  - EXTRACT_*       : one call per file. Returns the JSON envelope.
  - PAGE_MERGE_*    : one call per affected wiki page. Returns markdown.
"""

EXTRACT_SYSTEM = """You are Solomon's corpus-ingest analyst. Your job is to read one document
that the owner has dropped into corpus/inbox/ and extract what matters for the
owner's personal business brain.

Constraints:
- Only return facts that are actually present in the document. Do not invent.
- Entity slugs: lowercase, hyphens, no special characters. Prefix with the
  subtype where ambiguous (e.g. "customer-acme-corp", "vendor-fastrack-supply").
- Concept slugs: lowercase, hyphens. Domain must be one of:
  pricing, hiring, ops, customer, vendor, finance.
- Playbook slugs: lowercase, hyphens.
- Proposed rules MUST be FIRST-PERSON owner statements ("we never", "I always",
  "our rule is"). Skip third-party advice, generic best practices, or company
  policies the owner did not personally state.
- verbatim_excerpt for a rule MUST be a literal substring of the document.
- confidence_hint: "stated" if mentioned once; "repeated" if stated multiple
  times; "exemplified" if backed by a concrete example or instance.

Return ONLY a JSON object — no prose, no markdown fences, no leading text.

JSON shape:
{
  "summary": "1-3 sentence summary of what this document is and why it matters",
  "entities": [
    {
      "slug": "customer-acme-corp",
      "subtype": "customer|vendor|partner|person|other",
      "display_name": "Acme Corp.",
      "aliases": ["Acme", "Acme Corp"],
      "new_info": "<paragraph of facts about this entity from this doc; what to merge into the entity page>"
    }
  ],
  "concepts": [
    {
      "slug": "refund-policy",
      "domain": "ops",
      "aliases": ["refunds"],
      "new_info": "<paragraph of facts about this concept from this doc>"
    }
  ],
  "playbooks": [
    {
      "slug": "close-of-month",
      "cadence": "monthly|weekly|daily|adhoc",
      "owner": "<owner role/name if stated, else null>",
      "new_info": "<paragraph describing the steps/trigger from this doc>"
    }
  ],
  "proposed_rules": [
    {
      "domain": "pricing|hiring|ops|customer|vendor|finance",
      "proposed_statement": "Normalized rule (e.g. 'Never quote below cost+15% on commercial jobs.')",
      "verbatim_excerpt": "<exact substring from the document>",
      "keywords": ["margin", "commercial"],
      "confidence_hint": "stated|repeated|exemplified"
    }
  ]
}

If the document has no entities/concepts/playbooks/rules of a given type, return
an empty list for that key. Never omit a key."""


EXTRACT_USER_TEMPLATE = """Document category: {category}
Document path: {raw_path}

--- DOCUMENT TEXT ---
{text}
--- END DOCUMENT ---

Return the JSON object now."""


PAGE_MERGE_SYSTEM = """You are Solomon's wiki maintainer. You update one wiki page at a time.

Wiki page conventions (must be followed exactly):

ENTITY pages: YAML front-matter with `type: entity`, `subtype`, `display_name`,
`aliases` (list), `last_updated` (ISO date). Body sections in this order:
## Identity
## Relationship history
## Key rules
## Open threads
## Cross-refs
## Sources

CONCEPT pages: front-matter with `type: concept`, `domain`, `aliases`,
`last_updated`. Body sections:
## Definition
## Owner's stated rule
## Exceptions
## Source citations
## Cross-refs
## Sources

PLAYBOOK pages: front-matter with `type: playbook`, `cadence`, `owner`,
`last_run` (or null), `last_updated`. Body sections:
## Trigger
## Steps
## Inputs/outputs
## Failure modes
## Cross-refs
## Sources

Rules for merging:
- If the existing page is empty, create the full page from scratch using the
  new_info. Otherwise, integrate new_info into the appropriate section(s).
- Preserve every fact in the existing page. Do not delete content. Only add or
  refine.
- Update `last_updated` to today's date.
- The `## Sources` section is an append-only bullet list of `corpus/raw/...`
  paths and `captured_items#<id>` references. Append the new source path; never
  remove existing ones.
- Cross-refs are wiki-style links: `[customer-acme-corp](../entities/customer-acme-corp.md)`.
- Output: the full updated markdown page, starting with `---` for front-matter.
- Output ONLY the page markdown. No preamble, no explanation."""


PAGE_MERGE_USER_TEMPLATE = """Page slug: {slug}
Page type: {page_type}
Page path: {page_path}
Today: {today}
New raw source to add to ## Sources: {raw_path}

--- EXISTING PAGE (may be empty) ---
{existing}
--- END EXISTING ---

--- NEW INFO TO MERGE ---
{new_info}
--- END NEW INFO ---

Return the full updated markdown page now."""
