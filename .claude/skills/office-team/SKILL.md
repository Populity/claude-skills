---
name: office-team
description: Use when the user asks to work "as a team", "like an office", with several agents, sub-agents, managers or directors ("работай в команде", "5 агентов", "субагенты", "проверяющие менеджеры", "генеральные директора"), or hands over a large multi-part task that splits into independent work packets. Assembles a fixed office hierarchy — 2 directors (two departments), 2 checking managers, 5 team leads with 3 sub-agents each — gives every unit a different task with owned files, and does not close until both managers have passed the result.
---

# Office Team — how to staff and run a big task

The user's standing instruction: on a large task, work like an office, not like
one person. The team is always the same shape:

```
            Director A (dept 1)        Director B (dept 2)       <- the main session plays both
                    \                       /
        Manager 1 (technical check)   Manager 2 (standards check)   <- run AFTER the leads, verify, never build
                    |
   Lead 1     Lead 2     Lead 3     Lead 4     Lead 5             <- 5 agents, each owns one work packet
   3 subs     3 subs     3 subs     3 subs     3 subs             <- 15 sub-agents, one sub-task each
```

24 roles. Every unit gets a **different** task; nobody works on someone else's files.

## Roles

| Role | Who | Does | Never does |
|---|---|---|---|
| **Directors A and B** | the main session (only it talks to the user) | split the job into 2 departments, write the 5 work packets, assign file ownership, merge, report to the user | write a packet's work themselves while a lead owns it |
| **Managers 1 and 2** | 2 reviewer agents, spawned after the leads finish | M1: is it *correct*? (run the code, test edge cases, check docs match code). M2: is it *to standard*? (repo conventions, checklists, counters, no dead links) | build features; they report findings with file:line and a fix |
| **Leads 1-5** | 5 agents, spawned in parallel in one message | own one packet; split it into 3 sub-tasks; spawn 3 sub-agents (or do the 3 sub-tasks in order if they cannot spawn); integrate and self-test | touch files outside their packet |
| **Sub-agents** | 3 per lead | one narrow sub-task each, returns the result to its lead | — |

## How to act when given such a task

1. **Directors plan (main session, no agents yet).** Name the two departments
   (e.g. "Engineering" and "Documentation & Release"). Write 5 work packets —
   each with a goal, the exact files it owns, its 3 sub-tasks, and an executable
   done-check (a command that must pass). Packets must not share files. Anything
   that depends on everyone's output (counters, registries, final integration)
   stays with the directors for the end.
2. **Spawn the 5 leads in one message, in parallel.** Each prompt stands alone:
   repo path, conventions, owned files, the 3 sub-tasks, the done-check, and
   "spawn 3 sub-agents if your tools allow it; otherwise do the 3 sub-tasks
   yourself, one after another, and say so". Leads report what changed and the
   done-check output.
3. **Directors integrate.** Read the reports, fix collisions, do the shared
   steps (registries, counters, CLAUDE.md), run every done-check again.
4. **Spawn the 2 managers in parallel.** Give them the full change list. M1
   checks correctness by running things; M2 checks standards. Each returns
   PASS or a numbered list of findings.
5. **Fix and re-check.** Route each finding to its owner (or fix it as a
   director), then re-run the manager that raised it. Up to 3 rounds; after
   that, report what is still open rather than looping.
6. **Report to the user** in their language: who did what (a short org chart
   with outcomes), what passed, what is still open. Commit and push only the
   reviewed result.

## Rules

- Directors are the only voice to the user; agents' reports are not shown, so
  relay what matters.
- Each spawn starts cold. Put everything the agent needs in its prompt; never
  write "as discussed".
- Parallel only where the work is independent; shared files serialize through
  the directors.
- If nested spawning is unavailable, the structure still holds: the lead runs
  its 3 sub-tasks itself, and the report says so. Do not pretend 15 sub-agents
  ran when they did not.
- Managers verify by running commands, not by reading and agreeing.
- Small tasks (one file, one fix) do not need the office. Say so and do them
  directly, unless the user explicitly asks for the team.
