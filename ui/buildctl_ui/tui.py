"""The terminal UI. Same read model, same levers, same ceremony as the web console.

Signals keep their shapes so they survive a 16-colour terminal: ■ stopped, ◆ waiting for you,
● clear, ◌ outcome unknown, ▬ in progress, · not started.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, Label, Static, TabbedContent, TabPane

from .actions import Build, Refused

# One colour per aspect, from the web console's dark palette. Shapes still carry the state on
# terminals that cannot show these colours.
DANGER, CAUTION, CLEAR, OCCUPIED, MUTED, INK = "#f0544a", "#e8b330", "#4cae62", "#5b8cff", "#97a39b", "#e6ebe5"
GLYPH = {"danger": ("■", DANGER), "caution": ("◆", CAUTION), "clear": ("●", CLEAR),
         "unknown": ("◌", MUTED), "occupied": ("▬", OCCUPIED), "pending": ("·", MUTED)}
THEME = Theme(name="interlocking", primary="#46514b", secondary="#2a322e", accent="#a9b4ad", foreground=INK,
              background="#111513", surface="#161b18", panel="#181d1a", warning=CAUTION, error=DANGER,
              success=CLEAR, dark=True)
WORDS = {"danger": "stopped", "caution": "waiting for you", "clear": "clear", "unknown": "outcome unknown",
         "occupied": "in progress", "pending": "not started"}
PRIMARY = ("start", "recover", "pause", "resume", "approve", "grant", "retry", "reconcile-accept", "reconcile-retry")
STOP_LEVERS = ("approve", "grant", "retry", "reconcile-accept", "reconcile-retry", "recover")
SETUP_LEVERS = ("trust", "isolation", "auth", "bootstrap")


def glyph(aspect: str) -> str:
    char, colour = GLYPH.get(aspect, ("?", "white"))
    return f"[{colour}]{char}[/]"


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    seconds = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60)}m ago"
    return f"{int(seconds // 86400)}d ago"


def build_aspect(m: dict[str, Any]) -> tuple[str, str, str]:
    st = m["state"]
    if m["state_error"] or not m["audit"]["ok"]:
        return "danger", "State unreadable", m["state_error"] or m["audit"]["error"] or ""
    if not m["bootstrapped"]:
        return "pending", "Not set up", "finish setup to create the build"
    code = (m["escalation"] or {}).get("code", "")
    if st["status"] == "running" and m["lock"]["held"]:
        return "occupied", "Running", f"phase {st['current_phase']} · {st['phase_step'].replace('_', ' ')}"
    if st["status"] == "running":
        return "unknown", "Run died", "no live process holds the build"
    if any(lever["action"] == "reconcile-retry" for lever in m["levers"]):
        return "unknown", "Outcome unknown", code
    if st["pending_approval"]:
        return "caution", "Waiting for you", code
    if st["status"] in ("human_escalation", "failed"):
        return "danger", "Stopped", code
    if st["status"] == "paused":
        return "caution", "Paused", "no new work starts"
    if st["status"] == "complete":
        return "clear", "Complete", f"{len(st['completed_phases'])} phases"
    return "clear", "Idle", f"{len(m['phases']) - len(st['completed_phases'])} phases to run"


def lever_key(lever: dict[str, Any]) -> str:
    argv = lever["argv"]
    return f"{lever['action']}:{argv[argv.index('--operation') + 1]}" if "--operation" in argv else lever["action"]


def lever_operation(lever: dict[str, Any]) -> str | None:
    argv = lever["argv"]
    return argv[argv.index("--operation") + 1] if "--operation" in argv else None


class Confirm(ModalScreen[str | None]):
    """Typed confirmation against the evidence. The nonce is sent by the console, never typed."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, lever: dict[str, Any]):
        super().__init__()
        self.lever = lever
        confirm = lever["confirm"]
        value = str(confirm["value"])
        self.want = value[:confirm["length"]] if confirm["length"] else value

    def compose(self) -> ComposeResult:
        confirm = self.lever["confirm"]
        value = str(confirm["value"])
        with Vertical(id="ceremony"):
            yield Label(f"[b]{escape(self.lever['label'])}[/b]")
            yield Static(escape(self.lever["summary"]), classes="muted")
            yield Static(f"[b u #c08ae6]{escape(self.want)}[/]{escape(value[len(self.want):])}", id="evidence")
            yield Label(escape(confirm["prompt"]))
            yield Input(placeholder=self.want[:3] + "…", id="typed")
            yield Static("The console sends the nonce it showed you, so a stale approval is refused.", id="hint",
                         classes="muted")
            with Horizontal(classes="buttons"):
                yield Button("Sign", id="sign", variant="primary", disabled=True)
                yield Button("Cancel", id="cancel")

    @on(Input.Changed, "#typed")
    def typed(self, event: Input.Changed) -> None:
        match = event.value.strip().lower() == self.want.lower()
        self.query_one("#sign", Button).disabled = not match
        self.query_one("#hint", Static).update(
            "Matches. Signing records your approval in the audit chain." if match
            else "Does not match the evidence yet." if event.value else "")

    @on(Input.Submitted, "#typed")
    def submitted(self) -> None:
        if not self.query_one("#sign", Button).disabled:
            self.dismiss(self.query_one("#typed", Input).value)

    @on(Button.Pressed, "#sign")
    def sign(self) -> None:
        self.dismiss(self.query_one("#typed", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class Lever(Vertical):
    """One lever with its interlocking table."""

    def __init__(self, lever: dict[str, Any], index: int):
        super().__init__(classes="lever" + ("" if lever["enabled"] else " locked"))
        self.lever, self.index = lever, index

    def compose(self) -> ComposeResult:
        lever = self.lever
        tags = (" [reverse] paid [/]" if lever["spends"] else "") + (" [reverse] runs detached [/]" if lever["detached"] else "")
        with Horizontal(classes="lever-head"):
            yield Static(f"[b]{lever['number']:02d}[/b]", classes="plate")
            yield Static(f"[b]{escape(lever['label'])}[/b]{tags}\n[dim]{escape(lever['summary'])}[/dim]", classes="lever-title")
            yield Button(lever["label"] if not lever["confirm"] else lever["label"] + "…",
                         id=f"lever-{self.index}", disabled=not lever["enabled"],
                         variant="primary" if lever["enabled"] and not lever["confirm"] else "default")
        lines = []
        for condition in lever["conditions"]:
            aspect = "clear" if condition["held"] is True else "danger" if condition["held"] is False else "unknown"
            label = escape(condition["label"])
            if condition["held"] is False:
                label = f"[{DANGER}]{label}[/]"
            detail = f"  [dim]{escape(condition['detail'])}[/dim]" if condition["detail"] else ""
            lines.append(f"  {glyph(aspect)} {label}{detail}")
        blocked = [c["label"].lower() for c in lever["conditions"] if c["held"] is False]
        if blocked:
            lines.append(f"  [dim]Locked by: {escape('; '.join(blocked))}.[/dim]")
        if lines:
            yield Static("\n".join(lines), classes="conds")


class BuildctlTUI(App[None]):
    TITLE = "buildctl"
    CSS = """
    Screen { background: $surface; }
    #bar { height: 3; padding: 0 1; background: #0b0e0d; color: #e8ece6; }
    #aspect { width: 1fr; content-align: left middle; }
    #facts { width: auto; content-align: right middle; color: #a9b4ad; }
    .muted { color: $text-muted; }
    .lever { border: solid $panel-lighten-2; padding: 0 1; margin: 0 0 1 0; height: auto; }
    .lever.locked { border: solid $panel; }
    .lever-head { height: auto; }
    .plate { width: 5; border: solid $foreground-muted; height: 3; content-align: center middle; }
    .lever.locked .plate { color: $text-muted; }
    .lever-title { width: 1fr; padding: 0 1; height: auto; }
    .lever-head Button { min-width: 18; }
    .conds { padding: 0 0 0 6; height: auto; }
    #stop { padding: 1 2; height: auto; margin: 0 0 1 0; }
    #stop.danger { border: heavy #f0544a; }
    #stop.caution { border: heavy #e8b330; }
    #stop.unknown { border: dashed #97a39b; }
    #line { height: auto; padding: 1 1; border: solid $panel-lighten-1; margin: 0 0 1 0; }
    .h2 { text-style: bold; margin: 1 0 0 0; height: auto; }
    #phase-detail { height: auto; padding: 0 1; }
    DataTable { height: auto; max-height: 30; }
    #ceremony { width: 72; height: auto; padding: 1 2; border: thick #c08ae6; background: $surface; }
    Confirm { align: center middle; }
    #evidence { margin: 1 0; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    #result { height: auto; padding: 0 1; }
    """
    BINDINGS = [
        Binding("1", "tab('line')", "Line"), Binding("2", "tab('stop')", "Stop"), Binding("3", "tab('phases')", "Phases"),
        Binding("4", "tab('ledger')", "Ledger"), Binding("5", "tab('setup')", "Setup"),
        Binding("r", "refresh", "Refresh"), Binding("q", "quit", "Quit"),
    ]

    def __init__(self, build: Build):
        super().__init__()
        self.build = build
        self.model: dict[str, Any] | None = None
        self.fingerprint = ""
        self.result = ""
        self.levers_by_number: dict[int, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="bar"):
            yield Static("", id="aspect")
            yield Static("", id="facts")
        with TabbedContent(initial="line-tab"):
            with TabPane("Line", id="line-tab"):
                yield VerticalScroll(id="line-view")
            with TabPane("Stop", id="stop-tab"):
                yield VerticalScroll(id="stop-view")
            with TabPane("Phases", id="phases-tab"):
                with VerticalScroll():
                    yield DataTable(id="phases", cursor_type="row", zebra_stripes=False)
                    yield Static("", id="phase-detail")
            with TabPane("Ledger", id="ledger-tab"):
                with VerticalScroll():
                    yield Static("", id="chain")
                    yield DataTable(id="ledger", cursor_type="row")
            with TabPane("Setup", id="setup-tab"):
                yield VerticalScroll(id="setup-view")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "interlocking"
        phases = self.query_one("#phases", DataTable)
        phases.add_columns("#", " ", "Phase", "Attempts", "Reviews", "Fixes", "Debates", "Cost", "Merge")
        ledger = self.query_one("#ledger", DataTable)
        ledger.add_columns("Seq", "Time", "Event", "Phase", "Detail")
        self.action_refresh()
        self.set_interval(2.0, self.action_refresh)

    def action_tab(self, name: str) -> None:
        self.query_one(TabbedContent).active = f"{name}-tab"

    @work(thread=True, exclusive=True, group="read")
    def action_refresh(self) -> None:
        model = self.build.model()
        fingerprint = json.dumps({**model, "generated_at": None,
                                  "isolation": {**model["isolation"], "age_hours": None}}, sort_keys=True, default=str)
        if fingerprint != self.fingerprint:
            self.fingerprint = fingerprint
            self.call_from_thread(self.render_model, model)

    async def render_model(self, m: dict[str, Any]) -> None:
        first = self.model is None
        self.model = m
        aspect, word, sub = build_aspect(m)
        self.query_one("#aspect", Static).update(f"{glyph(aspect)} [b]{escape(word)}[/b]  [dim]{escape(sub or '')}[/dim]")
        iso = m["isolation"]
        self.query_one("#facts", Static).update(
            ("[b]╌DEMO╌[/]  " if m.get("demo") else "") +
            f"${m['budget']['spent_today']:.2f} of ${m['budget']['limit']:.2f} today   "
            f"{glyph('clear' if iso['fresh'] else 'caution')} sandbox {ago(iso['verified_at'])}   "
            f"{glyph('clear' if m['trust']['ok'] else 'danger')} trust {escape((m['trust'].get('governance_sha') or 'none')[:8])}   "
            f"lock {'pid ' + str(m['lock']['pid']) if m['lock']['held'] else 'free'}")
        self.sub_title = f"{m['repo']['path']} · generation {m['state']['generation']}"
        self.levers_by_number = {index: lever for index, lever in enumerate(m["levers"])}
        await self.render_line(m)
        await self.render_stop(m)
        self.render_phases(m)
        self.render_ledger(m)
        await self.render_setup(m)
        if first and not m["bootstrapped"]:
            self.action_tab("setup")
        elif first and m["escalation"]:
            self.action_tab("stop")

    def lever_widgets(self, m: dict[str, Any], actions: tuple[str, ...], *, enabled_first: bool = True,
                      always: tuple[str, ...] = ()) -> list[Lever]:
        chosen = [(i, lever) for i, lever in enumerate(m["levers"]) if lever["action"] in actions
                  and (lever["enabled"] or lever["action"] in always or actions != PRIMARY)]
        if enabled_first:
            chosen.sort(key=lambda item: not item[1]["enabled"])
        return [Lever(lever, index) for index, lever in chosen]

    def line_text(self, m: dict[str, Any]) -> Text:
        track, numbers, signals = Text(), Text(), Text()
        phases = m["phases"]
        cleared = 0
        while cleared < len(phases) and phases[cleared]["aspect"] == "clear":
            cleared += 1
        # The cleared stretch from phase 0 is one counted segment, as on the web line.
        if cleared >= 3:
            label = f" {phases[0]['number']}–{phases[cleared - 1]['number']} clear "
            track.append("━━" + label + "━━", style=INK)
            signals.append(" " * (len(label) + 4))
            numbers.append(" " * (len(label) + 4))
            phases = phases[cleared:]
        offset = len(track.plain)
        for phase in phases:
            aspect = phase["aspect"]
            char, colour = {"clear": ("━━", INK), "pending": ("╌╌", MUTED)}.get(aspect, ("██", GLYPH[aspect][1]))
            if phase["milestones"]:
                char = char[0] + "▣"
            track.append(char, style=colour)
            if phase["checkpoint"]:
                current = phase["number"] == m["state"]["current_phase"]
                signal = ("caution" if current and m["state"]["pending_approval"] else
                          "clear" if aspect == "clear" or current else "danger")
                signals.append(GLYPH[signal][0], style=GLYPH[signal][1]).append(" ")
            else:
                signals.append("  ")
            label = str(phase["number"])
            numbers.append(label.ljust(2) if phase["number"] % 5 == 0 or aspect not in ("clear", "pending") else "  ",
                           style="bold" if aspect not in ("clear", "pending") else "dim")
        stations = Text()
        for position, phase in enumerate(phases):
            for name in phase["milestones"]:
                column = offset + position * 2 + 1
                label = f"▲ {name.upper()} · {phase['number']}"
                stations.append(" " * max(1, column - len(stations.plain)))
                stations.append(label, style="bold")
        result = Text()
        result.append_text(signals).append("\n").append_text(track).append("\n").append_text(numbers)
        if stations.plain:
            result.append("\n").append_text(stations)
        result.append(f"\n{len(m['state']['completed_phases'])} of {len(m['phases'])} phases clear · ▣ milestone",
                      style=MUTED)
        return result

    async def render_line(self, m: dict[str, Any]) -> None:
        view = self.query_one("#line-view", VerticalScroll)
        await view.remove_children()
        widgets: list[Any] = []
        if m["escalation"]:
            widgets.append(self.stop_panel(m, compact=True))
        widgets.append(Static(self.line_text(m), id="line"))
        widgets.append(Static("Next move  [dim]a lever unlocks only when every condition holds[/dim]", classes="h2"))
        widgets.extend(self.lever_widgets(m, PRIMARY, always=("start", "recover")))
        if self.result:
            widgets.append(Static(self.result, id="result"))
        await view.mount(*widgets)

    def stop_panel(self, m: dict[str, Any], compact: bool) -> Static:
        e = m["escalation"]
        aspect, word, _ = build_aspect(m)
        number = m["state"]["current_phase"]
        phase = next((p for p in m["phases"] if p["number"] == number), None)
        where = f"   phase [b]{number}[/b] · {escape(phase['name'])} · attempt {phase['attempts']}" if phase else ""
        text = (f"{glyph(aspect)} [b]{escape(e['code'])}[/b]{where}\n{escape(e['meaning'])}\n"
                f"[b]What to do:[/b] {escape(e['remedy'])}")
        if not compact and e.get("detail"):
            text += f"\n\n[dim]{escape(e['detail'])}[/dim]"
        if compact:
            text += "\n[dim]Press 2 for the evidence and levers.[/dim]"
        return Static(text, id="stop", classes=aspect)

    async def render_stop(self, m: dict[str, Any]) -> None:
        view = self.query_one("#stop-view", VerticalScroll)
        await view.remove_children()
        if not m["escalation"]:
            _, word, _ = build_aspect(m)
            await view.mount(Static(f"Nothing is stopped. The build is {word.lower()}.\n[dim]When buildctl stops, this tab "
                                    "explains the code, shows the evidence, and offers only the levers the kernel will accept.[/dim]"))
            return
        widgets: list[Any] = [self.stop_panel(m, compact=False)]
        approval = m["state"]["pending_approval"]
        if approval:
            widgets.append(Static("What you are signing", classes="h2"))
            widgets.append(Static(f"  checkpoint   {escape(approval['checkpoint'])}\n  attempt      {approval['phase_attempt']}\n"
                                  f"  tree hash    {approval['tree_hash']}\n  governance   {approval['governance_sha']}"))
        review = (m["current"] or {}).get("review")
        findings = [f for f in (review["findings"] if review else m["findings"]["open"])
                    if f.get("status") not in ("resolved", "superseded")]
        if findings:
            order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
            findings.sort(key=lambda f: order.get(f["severity"], 9))
            widgets.append(Static(f"Findings  [dim]{len(findings)} open{' · ' + review['file'] if review else ''}[/dim]", classes="h2"))
            colour = {"CRITICAL": f"bold {DANGER}", "HIGH": DANGER, "MEDIUM": CAUTION, "LOW": MUTED}
            lines = []
            for f in findings:
                lines.append(f"  [{colour.get(f['severity'], 'default')}]{f['severity']:<8}[/] [b]{escape(f['claim'])}[/b]\n"
                             f"           [dim]{escape(f['path'])}:{f.get('line', '')} · {escape(f['kind'])} · {escape(f.get('status', 'open'))}[/dim]")
                if f.get("required_change"):
                    lines.append(f"           change: {escape(f['required_change'])}")
            widgets.append(Static("\n".join(lines)))
        widgets.append(Static("Levers  [dim]only what the kernel will accept[/dim]", classes="h2"))
        levers = self.lever_widgets(m, STOP_LEVERS)
        widgets.extend(levers or [Static("No lever applies. Fix the cause outside buildctl, then come back.")])
        if self.result:
            widgets.append(Static(self.result, id="result"))
        await view.mount(*widgets)

    def render_phases(self, m: dict[str, Any]) -> None:
        table = self.query_one("#phases", DataTable)
        cursor = table.cursor_row
        table.clear()
        for p in m["phases"]:
            table.add_row(str(p["number"]), Text.from_markup(glyph(p["aspect"])),
                          p["name"] + ("  [checkpoint]" if p["checkpoint"] else ""),
                          str(p["attempts"] or ""), str(p["reviews"] or ""), str(p["remediations"] or ""),
                          str(p["debates"] or ""), f"${p['cost_usd']:.2f}" if p["cost_usd"] else "", p["merge_policy"],
                          key=str(p["number"]))
        if cursor is not None and cursor < table.row_count:
            table.move_cursor(row=cursor)
        self.show_phase(cursor if cursor is not None else 0)

    @on(DataTable.RowHighlighted, "#phases")
    def phase_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_phase(event.cursor_row)

    def show_phase(self, row: int) -> None:
        if not self.model or row >= len(self.model["phases"]):
            return
        p = self.model["phases"][row]
        ops = sorted(p["operations"], key=lambda o: o.get("started_at") or "")
        lines = [f"{glyph(p['aspect'])} [b]{p['number']} · {escape(p['name'])}[/b]  [dim]{WORDS[p['aspect']]}[/dim]"]
        status_aspect = {"completed": "clear", "started": "occupied", "unknown": "unknown", "failed": "danger"}
        for op in ops:
            lines.append(f"  {glyph(status_aspect.get(op['status'], 'pending'))} {op['kind']:<14} {op['status']:<10} "
                         f"[dim]{(op.get('output_digest') or '')[:12]}[/dim]")
        if not ops:
            lines.append("  [dim]No agent calls recorded.[/dim]")
        self.query_one("#phase-detail", Static).update("\n".join(lines))

    def render_ledger(self, m: dict[str, Any]) -> None:
        audit = m["audit"]
        self.query_one("#chain", Static).update(
            f"{glyph('clear' if audit['ok'] else 'danger')} [b]{'Chain intact' if audit['ok'] else 'Chain broken'}[/b]"
            f"  {audit['sequence']} events  [dim]head {audit['head'][:16]}[/dim]")
        table = self.query_one("#ledger", DataTable)
        table.clear()
        for event in reversed(self.build.audit(max(0, audit["sequence"] - 300), 300)):
            payload = dict(event["payload"])
            phase = payload.pop("phase", "")
            if event["event_type"] == "checkpoint_approved":
                detail = Text.from_markup(f"[b #c08ae6 reverse] SIGNED [/] {escape(str(payload.get('scope', {}).get('checkpoint', '')))}")
            else:
                detail = "  ".join(f"{k}={v}" for k, v in payload.items())[:110]
            name = Text(event["event_type"], style=f"bold {DANGER}" if event["event_type"] == "human_escalation" else
                        CLEAR if event["event_type"] == "phase_completed" else "")
            table.add_row(str(event["sequence"]), event["timestamp"][11:19], name, str(phase), detail)

    async def render_setup(self, m: dict[str, Any]) -> None:
        view = self.query_one("#setup-view", VerticalScroll)
        await view.remove_children()
        t, iso = m["trust"], m["isolation"]
        rows = [
            ("danger" if m["config"]["error"] else "clear", "build.yaml", m["config"]["error"] or f"valid, from the {m['config']['source']}"),
            ("clear" if t["ok"] else "danger", "Trust anchor",
             f"pinned to {t['governance_sha'][:12]}, {len(t['governed_files'])} governed files" if t["ok"] else t["error"] or "not initialized"),
            ("clear" if iso["fresh"] else "caution" if iso["verified_at"] else "danger", "Sandbox",
             f"verified {ago(iso['verified_at'])}" if iso["fresh"] else iso["error"] or "not verified"),
            ("unknown", "Agent logins", "checked when each agent call starts"),
            ("clear" if m["bootstrapped"] else "pending", "Build state",
             f"created, generation {m['state']['generation']}" if m["bootstrapped"] else "not created yet"),
        ]
        text = "\n".join(f"  {glyph(a)} [b]{name:<14}[/b] {escape(str(what))}" for a, name, what in rows)
        widgets: list[Any] = [Static("Readiness  [dim]in the order buildctl checks it[/dim]", classes="h2"), Static(text),
                              Static("Levers", classes="h2")]
        widgets.extend(self.lever_widgets(m, SETUP_LEVERS, enabled_first=False))
        await view.mount(*widgets)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if not event.button.id or not event.button.id.startswith("lever-"):
            return
        lever = self.levers_by_number.get(int(event.button.id.split("-", 1)[1]))
        if not lever or not lever["enabled"]:
            return
        if lever["confirm"]:
            self.push_screen(Confirm(lever), lambda typed: self.pull(lever, typed) if typed else None)
        else:
            self.pull(lever, "")

    @work(thread=True, exclusive=True, group="pull")
    def pull(self, lever: dict[str, Any], typed: str) -> None:
        generation = self.model["state"]["generation"] if self.model else -1
        try:
            job = self.build.pull(lever["action"], generation=generation, confirm=typed, operation=lever_operation(lever))
            command = "buildctl " + " ".join(lever["argv"][:1])
            if job.detached:
                self.result = f"[{OCCUPIED}]▬[/] Started {command} as pid {job.pid}. It keeps running if you quit."
            elif job.returncode == 0:
                stamp = " [b #c08ae6 reverse] SIGNED [/]" if lever["confirm"] else ""
                self.result = f"[{CLEAR}]●[/] {escape(lever['label'])} done.{stamp}\n[dim]{escape(job.output[-600:])}[/dim]"
            else:
                self.result = f"[{DANGER}]■[/] {escape(lever['label'])} exited {job.returncode}.\n[dim]{escape(job.output[-600:])}[/dim]"
        except Refused as exc:
            self.result = f"[{DANGER}]■[/] {escape(str(exc))}"
        self.fingerprint = ""
        self.call_from_thread(self.notify, Text.from_markup(self.result.split("\n")[0]), timeout=6)
        self.action_refresh()


def run(build: Build) -> None:
    BuildctlTUI(build).run()
