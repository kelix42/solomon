# Solomon

**A personal chief of staff that learns how you make decisions, then slowly starts making them for you.**

Solomon watches the choices you make in your business — over emails, meetings, contracts, voice notes. It writes down the rules you actually live by, including the ones you've never said out loud. At first it just observes. Once it has enough evidence, it starts drafting replies the way you'd write them and flagging things you'd want to know about. After that, it starts handling things on its own: scheduling, replying, approving small expenses, declining the wrong-fit requests, escalating only what genuinely needs you.

You don't have to keep granting permissions one at a time. Solomon promotes itself when it gets things right, and demotes itself when it doesn't. A business that keeps running whether you're at your desk or not. Less time being the operator, more time being the owner.

It's a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). If you already use Hermes, Solomon turns it into something focused on one specific person and one specific business: yours.

## Who this is for

One business owner running one business. Solomon is built for a single person who makes a lot of decisions, has more context in their head than they can write down, and would like some of that context to live somewhere outside their head.

It is not built for teams sharing one instance, not built for general assistant use, and not built for people who want an LLM with extra steps. The whole point is that it gets to know *you*.

## How it works, in plain terms

Solomon has three jobs.

**1. It listens.** Every message, voice note, email, or file you point it at gets read, stored, and indexed so it can be searched later by meaning, not just keywords. Personal stuff like phone numbers, emails, and ID numbers is stripped out before anything is stored. Files you mark sensitive don't get indexed at all.

**2. It thinks before it speaks.** When something needs a decision, Solomon does two things in parallel:
   - A fast guess based on rules it already knows about you
   - A slower, more careful answer that pulls in your principles, your history, and the full situation

   It compares the two. The gap between them tells it how confident to be. Then a third pass (the "audit gate") checks the answer against your non-negotiables before anything leaves the system.

**3. It earns its own trust, gradually.** Every kind of decision (replying to a customer, scheduling a meeting, approving a small expense) has four levels:
   - **Watch** — Solomon just observes
   - **Suggest** — Solomon tells you what it would do
   - **Act with approval** — Solomon drafts the action; you click yes
   - **Act alone** — Solomon does it without asking

   Scopes start at "watch." Solomon moves them up on its own as it builds a track record, and drops them back down on its own when it gets something wrong. You don't have to manually promote each scope. You can override whenever you want, but most of the trust-ladder movement happens without you in the loop. Time alone never raises trust; only evidence does.

While you sleep, twelve background jobs run: comparing predictions against what actually happened, archiving rules that haven't been used, looking for contradictions in your principles, backing up your data, and queuing things for you to review next time you check in.

## What you need to do

There are three things to do, in order. The first two are required before Solomon does anything useful. The third is optional but recommended.

### Step 1 — Install

```bash
curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash
```

The installer detects or installs Hermes, installs Solomon as a plugin, sets up storage on your laptop (SQLite by default — a single file at `~/.hermes/solomon/solomon.db`, no database server needed), runs the schema, and registers the nightly job.

If you'd rather use Postgres, set `SOLOMON_DB_URL=postgresql://...` before running the installer. SQLite is the default because it just works.

### Step 2 — The onboarding interview

You sit down with Solomon for seven sessions, roughly an hour each, spread over a week or two. Each one is a conversation, not a form. Solomon asks open questions; you answer however feels natural; Solomon writes the answers down in a way it can use later.

```bash
solomon onboard session_0   # Industry — what business you're in, who your customers are
solomon onboard session_1   # Belief system — how you see the world
solomon onboard session_2   # Why — what you're actually trying to build
solomon onboard session_3   # Principles — your decision rules
solomon onboard session_4   # Ideal outcomes — what "good" looks like
solomon onboard session_5   # Non-negotiables — things you will never do
solomon onboard session_6   # Scopes — which kinds of decisions you'd want help with
```

You can stop and resume at any time. If you walk away mid-session, the next time you run the command it picks up exactly where you left off.

The output of these seven sessions lives in `~/.hermes/solomon/foundation/` as seven YAML files. You can open them and edit them by hand — Solomon will notice and flag the changes for you to confirm next time you check in.

### Step 3 — The historical dump (optional but powerful)

This is where you give Solomon the years of context the interview can't cover. Old emails, contracts, transcripts of customer calls, SOPs, anything that records decisions you've already made. The more you feed it, the faster it learns your patterns.

```bash
solomon corpus ingest path/to/old/emails/*.eml \
                      path/to/proposals/*.pdf \
                      path/to/transcripts/*.txt
```

You can also point it at a folder and let the watcher pick up new files as they land:

```bash
solomon corpus watch    # long-running; leave it open in a terminal or run as a service
```

For files you don't want Solomon to learn from at all (medical records, family stuff, anything legally sensitive), use the sensitive flag and they'll be stored but not indexed or learned from:

```bash
solomon corpus ingest --flag-sensitive path/to/private/file.pdf
```

Once Solomon has processed your corpus, it'll have spotted patterns and proposed rules it thinks you live by. You review those one by one:

```bash
solomon mentoring review
```

For each proposal: approve, reject, edit, or skip. Only the ones you approve become active rules.

## Day-to-day use

After install and onboarding, every Hermes conversation flows through Solomon. You don't need to do anything special — open Hermes, talk to it, and Solomon is listening underneath.

A few commands you'll use regularly:

| Command | What it does |
|---|---|
| `solomon doctor` | Health check — confirms storage, plugin registration, cron job all look right |
| `solomon corpus stats` | How many documents, chunks, and rules Solomon has |
| `solomon corpus lint` | Looks for broken references, orphan embeddings, files Solomon thinks it has but doesn't |
| `solomon mentoring review` | Walk through anything Solomon has queued for you to look at |
| `solomon sleep` | Run the nightly cycle on demand instead of waiting for 2 a.m. |

## Private mode

Sometimes you want to use the LLM for something unrelated to the business — a personal question, helping a friend, whatever. Type `/private` in any conversation. Nothing in that conversation gets logged, indexed, classified, or learned from until you toggle it off or end the session.

**Private means private.** There is no undo. If you forget you're in private mode and have a real business conversation, that data is gone forever. We chose this on purpose — the cost of an occasional forgotten conversation is much smaller than the cost of you not trusting the off switch.

One thing still runs in private mode: the non-negotiable check. Private mode turns off *learning*, not the guardrails that stop Solomon from doing something you've told it never to do.

## Where your data lives

Everything Solomon knows about you lives in one folder on your machine: `~/.hermes/solomon/`. Inside:

- `solomon.db` — the main database (a single SQLite file)
- `foundation/` — the seven YAML files from the interview
- `corpus/raw/` — the original documents you've ingested
- `corpus/wiki/` — a structured summary Solomon has built from those documents
- `backups/` — nightly tarballs of the corpus, kept for 30 days

You can back up Solomon by copying that one folder. You can move Solomon to a new machine by copying that one folder. If you ever want to start over, deleting that folder is the whole uninstall.

Local embeddings are computed on your machine using a free 384-dimension model (`sentence-transformers/all-MiniLM-L6-v2`). No external API calls happen during ingestion unless you explicitly opt into OpenAI embeddings.

The only piece that ever calls out to the network is the actual reasoning step (talking to an LLM provider like Anthropic or OpenAI), and that's the same call Hermes was making before Solomon was installed. Solomon doesn't add any new "phone home."

## Status

Solomon is feature-complete for v1. The interview engine, the corpus pipeline, the ten-stage decision pipeline, the conductor, the mentoring review CLI, and all twelve nightly jobs are built and tested (351 tests passing on SQLite as of 2026-05-26).

The next milestones are real-world: running the first foundation interview live, ingesting a real corpus pack, and watching scopes climb the autonomy ladder over the first 30 days of observe-only mode.

See `BUILD-STATE.md` for the current detailed state of the build.

## How Solomon stays compatible with Hermes

Solomon only talks to Hermes through the public plugin contract: `register_tool`, `register_command`, and the standard hooks (`pre_llm_call`, `post_llm_call`, `on_session_start`, and a few others). Hermes commits to keeping that contract stable, so Solomon updates and Hermes updates can happen independently.

The one file that actually touches Hermes is `solomon/adapter.py`. If anything in Hermes ever changes shape, that's the only file we update. The rest of Solomon — the decision pipeline, the sleep cycle, the audit gate, all of it — never knows what version of Hermes it's running on.

## Kill switch

If anything goes sideways in production, there's a one-line recovery:

```bash
echo SOLOMON_PIPELINE_DISABLE=1 >> ~/.hermes/.env
hermes restart
```

That flips Solomon's decision pipeline off and falls back to the original Hermes behavior. No uninstall, no rollback, no code change. Confirm recovery by sending a message; if Hermes responds normally, you're back to baseline.

## License

MIT. See LICENSE.

## Credits

Built on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
