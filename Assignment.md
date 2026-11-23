Agent Core Assignment
23 Nov 2026 · @Someone
Thanks for making it to this round. This page has everything you need: what to build, how to use your API key, the starter repo, what to hand in, and how we'll judge it.
You'll build agentcore: a small, reusable agent runtime, plus a team of agents on top of it that runs the WhatsApp front desk of a fictional hospital. It's close to the real work you'd do with us in your first weeks.
At a glance

Work expected
1 to 2 days (about 10 to 16 hours)
Deadline
48 hours from when we send your API key. The exact time comes with the key
Stack
Python 3.11+, FastAPI, Pydantic, the Anthropic Python SDK. No agent frameworks
Tools
Claude Code only, with the API key we email you
Budget
$30 of Claude credit for everything
Hand-in
Your GitHub repo, public or private with Fire-Dolphin invited, including your Claude Code sessions. You send us the link by email
After you submit
We review your repo first. If it clears our bar: a 20-minute call with a short demo and questions about your work
What we're looking for:
Skill
Where it shows
You understand how an agent works
You turn a naive agent loop into a proper runtime: tool registry, step limit, retries, hooks
You design reusable code
The runtime works for any tools and agents, not just hospital ones
You can build multi-agent systems
A cheap router model sends each message to a specialist agent, and context carries across handoffs
You put safety in code, not just prompts
Patients only touch their own data, nothing changes without a yes, emergencies stop everything
You handle real-world failure
The hospital system is slow, throws errors, and sometimes fails after saving a booking
You measure your own work
Eval runs with pass rates, traces with cost per conversation
You work well with Claude Code
Your CLAUDE.md, your Claude Code sessions, and how you used the budget
How it works
1.  We email you this page, your API key and your exact deadline. Your 48 hours start the moment that email is sent, not when you open it. Everything else happens by email too.
2.  Make your own copy of the starter repo (see The starter repo, below). Keep your copy public, or make it private and invite our GitHub account, Fire-Dolphin, as a collaborator.
3.  Build it with Claude Code and that key. Commit and push as you go: small commits with clear messages, not one big commit at the end.
4.  Export every Claude Code session into claude-sessions/ before you clear or close it (see Using Claude Code, below).
5.  Before the deadline: run tools/pack_sessions.py, push, and reply to our email with your repo link and a short summary (see the hand-in list below).
6.  After you reply, or at the deadline if that comes first, stop. No more commits or pushes to your repo. We check, and any change after that point fails your submission. Your key also switches off at the deadline.
7.  We review your repo first. If it clears our bar, we email you to set up a 20-minute call: a short demo, then our questions about your work. We switch your key back on for it.
The assignment
The situation. City Care Hospital wants an AI front desk on WhatsApp. Patients book, reschedule and cancel appointments, check their bills, and ask simple questions. We already have a starter repo with a fake hospital system and a naive agent that works on a good day and breaks on a bad one. Your job is to replace that naive agent with a proper, reusable agent runtime and a set of agents built on it.
Read context.md in the starter repo first. It explains the product, the rules, the response format and the hospital system.
Part 1: the runtime (agentcore/). A small Python package that knows nothing about hospitals. We should be able to reuse it for any agent. It must have:
1.  A tool registry. Register a Python function with a Pydantic input model; the registry builds the tool definition for Claude, checks the model's input before running the tool, and turns tool errors into results the model can see.
2.  An agent loop written by you with client.messages.create. It has a step limit, timeouts, retries with backoff when the API is rate-limited or down, and handles every stop reason (including running out of tokens and refusals).
3.  Hooks around each tool call and each model call. A hook can let the call run, block it with a reason, or pause it until the user confirms.
4.  A trace of every run: each model call and tool call, with inputs, outputs, time taken, tokens and cost.
5.  A session store behind a small interface. Keeping it in memory is fine, but we should be able to swap in Redis later without touching the loop.
Part 2: the agents, built on agentcore.
• A router on claude-haiku-5-5 that picks who handles each message: booking, billing, info, or a human.
• A booking agent: find doctors, check free slots, book, reschedule, cancel.
• A billing agent: show the patient's bills and send a payment link.
• An info agent that answers only from hospital_info.md, and says so when the answer isn't there.
• Handoffs: if a patient changes topic mid-chat ("also, how much do I owe?"), the right agent takes over without asking again for what is already known.
Part 3: safety, enforced in code (hooks), not only in prompts.
• A patient can only see or change their own appointments and bills (matched by the phone in the request). The hospital system does not check this. That's on purpose.
• Nothing changes (book, reschedule, cancel, payment link) without reading the details back and getting a clear yes.
• An emergency check runs before the router on every message. On an emergency: stop, tell them to call 108 or go to the nearest emergency room, set emergency: true, and give no medical advice.
• Cancelling or rescheduling less than 4 hours before the slot goes to hospital staff (handoff: true).
Part 4: a bad day. Start the hospital system in chaos mode (see the README): it's slow, 10% of calls fail, a slot can be taken while you book it, and sometimes a booking is saved but the call still returns an error. The agent must never tell a patient something is booked unless it is, and must never book twice.
Part 5: prove it works.
• Run the 10 eval scenarios in evals/ 3 times in chaos mode and report the pass rate of each run. Add 3 scenarios of your own.
• pytest must pass without an API key (a fake Claude client is in tests/).
• Expose the trace at GET /trace/{session_id}.
Part 6: DESIGN.md (1 to 2 pages). How agentcore works and why you shaped it that way; what the model decides vs what your code decides; how you handle each failure; which model each agent uses, with cost and speed numbers to back it up; eval results and the cases that still fail; what you'd change for phone calls instead of chat; and what's missing before real patients use it.
Nice to have, only if the must-haves are done: prompt caching, streaming replies, a Redis session store, a Dockerfile, a tiny chat page.
Rules
• Build it with Claude Code and the API key we send you (set it as ANTHROPIC_API_KEY). The same key powers your agents. Don't use other AI coding tools for this assignment (Cursor, Copilot, ChatGPT, Codex and the like).
• Your budget is $30 for everything. Use Sonnet in Claude Code (/model sonnet) and check spend with /cost. Spending wisely is part of the assignment.
• Python 3.11+, FastAPI, Pydantic, the anthropic SDK. No agent frameworks (LangChain, LangGraph, CrewAI, LlamaIndex, Pydantic AI, OpenAI Agents SDK, AutoGen, the Claude Agent SDK) and not the SDK's tool runner helper. We want to see your loop.
• Router on claude-haiku-5-5. For the specialists, choose claude-sonnet-5-5 or claude-haiku-5-5 and justify it with numbers.
• Read mock_hospital/, but don't change it or the 10 given scenarios. You may add check types to evals/run_evals.py for your own scenarios, but don't change how the existing ones work.
• Your repo can be public, or private with Fire-Dolphin invited as a collaborator. Either way, you send us its link. Never commit the key.
• Write proper commit messages. Each one says what changed and why, for example "Add idempotency key to booking writes so a retry can't double-book". We read your history. Messages like "fix", "update" or "wip", or one big commit at the end, count against you.
• You may not finish everything. If so, do the most important parts well and say in DESIGN.md what you skipped and why. That counts in your favour.
• This is a hiring exercise. We won't use your code in our product unless we hire you.
Hand in before your deadline. Your repo must contain:
1.  Your code, with the README updated so we can run your version.
2.  Your CLAUDE.md, plus any other files you wrote to guide Claude Code.
3.  DESIGN.md.
4.  EVALS.md: the pass rate of each of your 3 chaos runs, and why each failing case fails.
5.  evals/reported/: the 3 result files (from evals/results/) that EVALS.md reports.
6.  Your 3 or more new scenarios in evals/scenarios/ (11_...yaml and up).
7.  claude-sessions/: one /export file per session, plus transcripts.zip (see Using Claude Code, below).
Push your final work, then reply to our email before the deadline with your repo link and a short summary in the email itself, 5 to 10 lines: what works, what you skipped and why, the pass rate of each of your 3 chaos runs, and roughly how many hours you spent. A late reply, or one without the repo link, counts as no submission.
From the moment you reply (or the deadline, if that comes first), your repo must not change: no commits, no pushes, no force-pushes. We check the commit history and GitHub's push log.
The starter repo
Repo link: github.com/Fire-Dolphin/citycare-agent
Clone or fork it into your own GitHub repo, and work there.
Your copy can be:
• public, or
• private, with our GitHub account Fire-Dolphin invited as a collaborator (your repo's Settings → Collaborators → Add people).
Either way, you send us its link when you reply to our email. If it's private and Fire-Dolphin isn't invited, we can't open it.
Path
What it is
context.md
Read this first. The product, the hospital rules, the hospital system's API and quirks, models and prices, how the evals work, and the seed data
hospital_info.md
The only facts the info agent may use
mock_hospital/
A fake hospital system (appointments and billing) on port 8001, with a chaos mode. Read it; don't change how it behaves
agent_service/
The FastAPI app with POST /chat, and naive_agent.py, the agent you'll replace
evals/
10 scenarios and a runner that checks what really happened in the hospital system
tests/
Example tests and fake_claude.py, a scripted Claude client so tests run without a key
README.md
Setup, how to run everything, how to run evals and tests
tools/pack_sessions.py
Packs your Claude Code transcripts for hand-in and checks the repo for API keys
Getting started (about 15 minutes):
1.  Clone your copy, run uv sync (or pip install -r requirements.txt), and copy .env.example to .env with your key.
2.  Read context.md, then point Claude Code at it, and write your CLAUDE.md before writing code.
3.  Start the hospital system and the naive agent, chat with it, then run python -m evals.run_evals --chaos to see how it does on a bad day.
4.  Plan, then build. Tip: you can test almost everything against the fake client and the mock without spending credit.
Your API key and budget
Your Claude API key comes in our email, with $30 of credit. It's yours for this assignment, and it powers both Claude Code and the agents you build.
Setup:
• Set it in your terminal before starting Claude Code: export ANTHROPIC_API_KEY=sk-ant-... (PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."). Put the same key in your .env for the agent service.
• If you already use a Claude subscription in Claude Code, it will ask once whether to use this key instead. Say yes. /status shows which account is active.
• Never commit the key, and don't paste it into prompts.
Making $30 last:
Spend on
Rough cost
Tip
Claude Code, 1 to 2 days
About $13 per active day, so $13 to $26
Use Sonnet (/model sonnet). Opus only for a short, hard planning step. Use /clear between unrelated tasks, after exporting the session
Your agents while building
$2 to $4
The router runs on Claude Haiku 5.5, which is very cheap
Eval runs (3 runs required)
$2 to $6
Depends on your specialist model and whether you use prompt caching
Check Claude Code's spend any time with /cost. How you use the budget is part of what we look at. Spending all of it isn't a problem; wasting it is.
If you run out, API calls start failing with a message about the usage limit. Email us. We can top it up once, and two full days of work may well need it.
The key stops working at your deadline. If we book a review call, we switch it back on for the call.
Using Claude Code (we read your sessions)
• Start Claude Code from the repo root, every time. Claude Code files sessions by folder, and the packing script looks there.
• The next day, claude --continue picks up where you left off. One long session is fine, and so are several.
• Before you /clear or close a session, export it, numbered in order: /export claude-sessions/01-planning.txt, then 02-..., and so on.
• At the end, run uv run python tools/pack_sessions.py. It zips the full transcripts Claude Code keeps on your machine (even the parts it compacted in a long session) into claude-sessions/transcripts.zip, and stops if it finds an API key in the repo. Then commit and push.
• Don't edit exports or transcripts. The only allowed edit is removing an API key from an export. If your key shows up in a transcript, tell us and we'll replace it.
• We check that it all adds up: your sessions, your commits and your key's usage. Code that's in your commits but in none of your sessions counts as not written by you.
The review call (20 minutes)
We set it up by email after we've reviewed your repo, and only if it clears our bar. Have the project running locally and be ready to share your screen.
Part
Time
What happens
Demo
8 min
Show a few chats, including one in chaos mode where something goes wrong and the agent recovers. Show a trace
Questions
12 min
About your code and design choices, your failing eval cases, and what you'd change for phone calls. Expect to open your code and show where things happen
There's nothing to prepare beyond knowing your own code well, including the parts Claude Code wrote.
What fails a submission
We grade strictly. Working code is where we start, not the goal. We rerun your evals, run scenarios you haven't seen, try to trick your agent, and read your sessions. Every claim in DESIGN.md and EVALS.md has to match what we find.
We stop reviewing, whatever else is good, if:
• an API key is anywhere in your repo or its git history
• you changed mock_hospital/ or the 10 given scenarios, or how an existing eval check works
• you used an agent framework or the SDK's tool runner
• agentcore/ imports hospital code
• your sessions are missing, or a real part of your code isn't in them
• pytest fails with no API key set
• we can't open your repo from the link you sent (a private repo without Fire-Dolphin invited)
• you commit or push to your repo after you've replied, or after the deadline
• your eval numbers don't hold up when we rerun them, and you can't explain why
How we'll evaluate it
Area
What we look for
Weight
The runtime
Clear interfaces, no hospital code inside, easy to add a new tool or agent, easy to test
8
The loop
Step limit, retries and timeouts, every stop reason handled, tool errors shown to the model
6
Safety in code
Own-data checks, confirm before changes, emergency check, the 4-hour rule. We'll try to trick it
7
A bad day
No false "booked", no double bookings, races and saved-but-failed writes handled
6
Router and handoffs
Right agent for each message, context carried over, no re-asking
5
Evals and traces
3 honest runs, failures explained, 3 good new scenarios, a useful trace with cost
4
DESIGN.md
Real trade-offs, numbers behind model choices, honest about what's weak
2
Working with Claude Code
A useful CLAUDE.md, sessions that show planning and checking, clear commit messages, sensible spend
2
Total

40
The review call counts too: you need to explain your code and your choices, including the parts Claude Code wrote, without getting lost.
Questions about the assignment? Ask us early, by email. Asking a good question is never a bad sign.
Good luck. We're looking forward to seeing what you build.
