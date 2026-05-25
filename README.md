# Solomon

**A personal business brain that learns how you decide, then starts deciding for you.**

Solomon is a plugin for [Hermes](https://github.com/NousResearch/hermes-agent). After you install it, every conversation you have with Hermes flows through the Solomon role. Solomon writes down how you think, who your customers are, how you handle vendors, how you price, how you decide. Over time it speaks more like you, knows more about your business, and starts proposing actions on emails and messages before you ask.

## Install

One command:

```bash
curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash
```

What this does:

1. Checks that Hermes is already installed (Solomon does NOT install Hermes for you — see [the Hermes README](https://github.com/NousResearch/hermes-agent) if you don't have it yet).
2. Installs Solomon as a Hermes plugin.
3. Creates one folder at `~/.hermes/solomon/` to hold everything Solomon knows about you.
4. Registers 17 background jobs with Hermes (1 nightly reflection, 14 weekly playbook compressions, 1 weekly summary regeneration, 1 weekly check-in).
5. Tells you to open Hermes and type `/onboard` to start.

That's the whole install. No database. No Docker. No prompts.

## Running cost

Solomon adds tokens to your existing Hermes spend — it doesn't replace it. The
range depends mostly on how many inbound messages flow through your gateways
and how often you talk to Hermes.

| Profile | Conversation turns/day | Inbound messages/day | Sonnet-equivalent monthly cost |
|---|---|---|---|
| **Light** — solo founder, occasional use | ~5 | ~5 | **$3–$5/mo** |
| **Typical** — daily use, real inbox flow | ~25 | ~30 | **~$26/mo** |
| **Heavy** — multi-gateway, high inbound volume | ~75 | ~100 | **$80+/mo** |

These are incremental — on top of whatever you already pay Hermes for the LLM
calls Solomon's role adds context to. The biggest single line item is the
weekly compression set (~$2–$8/week depending on how full your playbooks are).
The daily reflection cron is the second-biggest. Both cap themselves: the
weekly compression set is 15 jobs that each cover one document; the daily
reflection is one job that runs once and exits.

Run `solomon logs --today --grep tokens` to see your actual usage.

## First steps

Open Hermes. You'll be on whatever channel you set Hermes up with (Telegram, the desktop chat, etc.). Type:

```
/onboard
```

Solomon will start the first of seven foundation interview sessions. Each session is a conversation, about thirty to sixty minutes. You can stop any time. The next time you type `/onboard` it picks up where you left off.

The seven sessions in order:

1. **Industry & sector** — what your business actually is
2. **Belief system** — how you see the world
3. **Why** — what you're trying to build
4. **Principles** — your decision rules
5. **Ideal outcomes** — what success and failure look like
6. **Non-negotiables** — things you will never do
7. **Scopes** — what kinds of decisions you'd want Solomon to help with

After session 7, Solomon is loaded and starts working alongside you.

## Day to day

You just use Hermes. Solomon is loaded by default. It speaks in your voice (the more you talk to it, the more it learns your phrases). It respects the rules you set. When an email comes in, Solomon reads it, decides what should happen, and proposes an action through your preferred channel. You approve, edit, or ignore.

Useful commands:

| Command | What it does |
|---|---|
| `/onboard` | Continue the foundation interviews |
| `/mentor` | Walk through pending items with Solomon (do this once a week) |
| `/status` | Show what's done, what's pending |
| `/private` / `/endprivate` | Turn learning off / on for the current conversation |
| `/ingest` | Process documents you dropped in `~/.hermes/solomon/inbox/` |
| `/reflect` | Run the nightly reflection now |
| `/solomon-off` / `/solomon-on` | Globally suspend / resume Solomon |

## Where your data lives

Everything is in one folder:

```
~/.hermes/solomon/
├── profile.yaml          # the foundation (filled by /onboard)
├── vocabulary.md         # your phrases, captured verbatim
├── customers.md          # who buys
├── vendors.md            # who supplies
├── operations.md         # how the day-to-day works
├── sales.md, marketing.md, finance.md, people.md, product.md,
├── support.md, legal.md, technology.md, strategy.md, procurement.md
├── review_queue.jsonl    # pending knowledge updates
├── pending_actions.jsonl # pending actions awaiting your approval
├── inbox/                # drop documents here
├── archive/              # processed documents and older versions
└── logs/                 # what Solomon did and when
```

To back Solomon up: copy that folder. To move to a new machine: copy that folder. To start over: delete that folder. The folder is a git repo so every change is reversible.

## Privacy

- All data lives on your computer in one folder.
- The only network call is Hermes talking to the LLM (the same call Hermes was making before Solomon).
- Sensitive identifiers (SSN, credit cards, phone numbers, emails) are automatically replaced with placeholders before anything lands in any file.
- `/private` is a hard off-switch for any conversation. Nothing said in private mode is logged or learned from.

## Troubleshooting

```bash
solomon doctor       # check that everything is wired right
solomon logs --today # see what Solomon did today
solomon logs --errors # see what went wrong
```

If something looks broken, those three commands tell you what and why.

## License

MIT. See [LICENSE](LICENSE).

## How it's built

See [SPEC.md](SPEC.md) for the full architecture. It is the source of truth.
