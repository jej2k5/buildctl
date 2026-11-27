## A real build, end to end {#end-to-end}

I gave buildctl a one-page request for a small app and left it alone. Eight minutes and $16 later, the code was written, checked, reviewed and documented, and the build was waiting for my signature. This chapter is everything that happened on that run, on 27 November 2026, including what went wrong, with the commands so you can repeat it. The times and costs come from buildctl's own logs.

AI agents write decent code now. What I couldn't do was leave one alone. An agent says it's done, and the proof is tests it wrote itself. It edits files nobody asked about. It doesn't stop when it should, and nobody counts what it spends. Afterwards, all I had was its word. buildctl sits between me and the agents. It locks a written plan, including what "done" means, before any code. It checks the work with tests the builder didn't write. A second AI reviews the work with read-only tools. The agents run in a sandbox. It stops at a daily spending limit, waits for my signature where the plan says to, and logs everything.

| Step | Time | Cost |
|---|---|---|
| The plan (my Claude subscription, outside buildctl's budget; the interviewer's reported usage, without the 12 review calls) | 43 min | $8.50 |
| The run, both pieces | 8 min 19 s | $16.00 |
| Recorded demo | 10 s | $0 |
| Release notes and demo script | 79 s | $4.00 |

### The plan {#e2e-discovery}

The test project was a bill splitter: bill, people and tip in, exact shares out. The shares add up to the total, and no one pays more than a cent extra. It needed a command line and a small local web page. I wrote that on one page, and wrote one more file by hand: the steps for the demo recording at the end.

```json
{"title": "Split a dinner bill three ways", "steps": [
  {"name": "The page opens", "open": "/", "expect": "Split the bill", "hold": 1.5},
  {"name": "Enter the total", "type": {"target": "css:#total", "text": "84.50"}},
  {"name": "Three people", "type": {"target": "css:#people", "text": "3"}},
  {"name": "Eighteen percent tip", "type": {"target": "css:#tip", "text": "18"}},
  {"name": "Split it", "click": "Split", "expect": "Each person pays", "hold": 3}]}
```

An AI interviewer turned my page into a plan through `buildctl-discover`:

```sh
buildctl-discover start
claude -p "Run discovery for the splitbill repository…" \
  --mcp-config mcp.json --allowedTools mcp__discover
```

It recorded every decision (18, of which 16 still stand after two were replaced), two pieces of work, and the checks each piece has to pass. It can't write the plan files; buildctl renders them from what it recorded. A separate, read-only Claude session reviewed the plan and sent it back with problems, twelve times over.

**A check that fails correct code costs money on every attempt. I found one before spending anything.** Before paying for a run, I wrote a quick throwaway version of the app and ran all 13 checks against it. Twelve passed. One banned `urllib.parse`, the standard module for reading the web form, and the instructions never mentioned the ban. A short third round of discovery fixed only that.

The reviewer caught a second one. I'd asked for a test that the web server answers on its port, but on macOS buildctl runs checks with the network off completely, so that test could never pass. I changed the plan: test the page's logic directly, and prove the live port with the recorded demo, which is allowed to serve locally. I caught a third problem myself. A browser's number box rejects `84.50` unless it's told to allow cents (`step="0.01"`), so the demo would have stalled on the first field.

`buildctl-ui --session default` [shows the plan](#console-discovery) while it is still a draft:

![The finished plan in the console: ready to finalize, two pieces of work and the stop after them, no open problems, and the interviewer's log ending with the open question parked and the plan finalized.](media/e2e-discovery.gif)

My one open question, whether to support uneven splits, I parked as an accepted risk; only the operator can do that. Then I locked the plan:

```sh
$ buildctl-discover finalize --confirm 16f65177
Wrote 8 files from proposal 16f6517796c8.
$ git commit -q -m "Roadmap from discovery"
$ buildctl trust init --to f343765e8db7… --confirm f343765e8db7…
$ buildctl isolation verify
{ "backend": "macos-sandbox-exec-v2-deny-default", … "status": "PASS", … }
$ buildctl auth check
{ "implementer": { "auth_method": "ChatGPT", "authenticated": true, … },
  "reviewer": { "auth_method": "claude.ai", "authenticated": true, … }, "status": "PASS" }
```

`trust init` records a fingerprint of the plan, the instructions, the exact agent programs and buildctl itself. If any of those change, work stops until someone looks. `isolation verify` checks that the sandbox actually holds on this machine. The first `finalize` refused because `build.yaml` wasn't committed yet; it won't overwrite a file Git can't restore.

![The console before the run: two pieces of work in plain words, a stop after the web page, nothing spent.](media/e2e-plan.gif)

### The run {#e2e-run}

```sh
buildctl start
```

I did something else. For each piece, Codex wrote the code and the checks ran in the sandbox. A documentation pass updated the README and changelog, then Claude reviewed the code and docs together, read-only. Only then did buildctl commit.

![Mid-run: the first piece being written, $2 spent, Start locked while work is running.](media/e2e-running.gif)

Both pieces were done in 8 minutes 19 seconds, for $16.

| Piece | What it took | Cost | Time |
|---|---|---|---|
| Core and command line | build, 2 fix-up rounds, docs, review | $10.00 | 4 min 49 s |
| Web page | build, docs, review | $6.00 | 3 min 28 s |

The first piece failed its check twice, and that was my fault. My starter files included an empty `tests/__init__.py`, and buildctl won't accept an empty required file. It sent the work back until the file had content, which cost $4. My throwaway run hadn't checked file sizes, so it missed this.

The reviewer also flagged a real risk. The number parser accepts numbers of any length, and the web page passes it whatever anyone types, so a huge number could tie up the server. It was logged as an open issue rather than a blocker, and it shows at sign-off. The web page passed on the first attempt, with one small note: its error message doesn't say which field was wrong.

The documentation pass left this in the changelog:

```markdown
### Added

- Added `python -m splitbill` to print individual shares and the total with tip.
- Added a Python API for parsing bill inputs and splitting bills in whole cents,
  with half-up tip rounding and leftover cents assigned to the first people.
- Added `python -m splitbill.web PORT` to serve a self-contained bill-splitting
  form on `127.0.0.1` that displays each person's share and the total with tip.
```

### Seeing it work {#e2e-demo}

```sh
$ buildctl-drafts record --milestone mvp
  ok    1. The page opens
  ok    2. Enter the total
  ok    3. Three people
  ok    4. Eighteen percent tip
  ok    5. Split it
Recording in …/drafts/20261127-193122/demo. Nothing was committed or published.
```

buildctl started the finished app from a clean copy of the code awaiting sign-off, in a sandbox where it can serve a local page and reach nothing else, and a browser locked to that address followed my steps. Ten seconds, no cost.

![The app the build produced: $84.50 for three at 18% comes to $33.24, $33.24 and $33.23, total $99.71.](media/e2e-product.gif)

### Signing off {#e2e-checkpoint}

The plan said to stop after the web page, so it stopped ([CHECKPOINT_REQUIRED](#CHECKPOINT_REQUIRED)). The sign-off screen lists exactly what I'm approving, down to a fingerprint of the code ("tree hash"). To sign, I type its first seven characters. That doesn't prove I read the code. It proves I'm approving this exact version, and if the code changes afterwards, the signature no longer counts.

![Waiting for sign-off: what is waiting, the evidence, and only the buttons that will work.](media/e2e-checkpoint.gif)

![The signature: the reviewer's open note, and the field for the first seven characters of the tree hash.](media/e2e-sign.gif)

### Release notes {#e2e-drafts}

Last, buildctl drafted release notes and a demo script. Each factual sentence has to cite the line it came from, and buildctl checks that the line exists. All 27 claims in the release notes checked out. The first:

> splitbill splits a restaurant bill between friends, tip included, down to the cent.
> (README.md, line 3)

Then my own tool told me no. I asked for four drafts. Four could cost up to $16, and only $9 of the day's $25 limit was left, so the console refused, and it also noted that `buildctl-drafts` wasn't installed in its Python. From the terminal, two drafts fit, for $4:

```sh
$ buildctl-drafts make --milestone mvp --only release-notes,demo-script
  release-notes  sourced    27 of 27 claims backed
  demo-script    sourced    31 of 31 claims backed
```

![The release notes with each claim's source beside it, and the button for more drafts locked, with its reasons.](media/e2e-drafts.gif)

### What I took from it {#e2e-lessons}

Run the plan's checks against a throwaway version before you pay; it found a check that would have failed correct code. It missed the empty-file rule, which cost $4, so model the file checks too. On macOS, write tests that don't need a network, and let the recorded demo prove the live parts. The spending limit applies to you too.

This run didn't hit the harder cases: two agents disagreeing about a design, a reviewer refusing to pass the work, a docs pass editing files it shouldn't. The [built-in demos](#demo) show each one for free: `buildctl-ui --demo debate`, `--demo blocked` and `--demo docs`.

The real question isn't whether AI can build it. It's whether you can walk away while it does.
