# Concurrent sessions — worktree playbook

Default: **single-session work stays directly on `main`.** No worktree, no branch, no
extra ceremony — just work, commit, done.

Only reach for a worktree when you are genuinely running **parallel sessions on
different threads at the same time** (e.g. two desktop sessions, or a background
agent plus a foreground session, both editing code concurrently). A worktree gives
each thread its own isolated working directory + branch so they can't stomp on each
other's uncommitted changes or collide mid-edit.

## Recipe

Start a thread:

```
git worktree add .claude/worktrees/<slug> -b thread/<slug>
```

Work inside that worktree in isolation — it's a full separate checkout, its own
branch, no interference with `main` or any other worktree.

When the thread's work is ready, merge it back explicitly:

```
git checkout main
git merge thread/<slug>
```

Then tear the worktree down:

```
git worktree remove .claude/worktrees/<slug>
git branch -d thread/<slug>
```

## Rules of thumb

- Don't create a worktree just to "be safe" for ordinary single-session work — that's
  the ceremony this doc is explicitly telling you to skip.
- Do merge back and remove the worktree promptly once the parallel work concludes.
  A worktree left sitting around unmerged/unremoved is exactly the kind of orphaned
  state that has bitten this repo before (see conductor open items for a live example)
  — don't add another one.
- `conductor/cli.py log`/`open` entries should note which `session_tag` (branch) did
  the work, so a later `render` shows which thread produced what.
