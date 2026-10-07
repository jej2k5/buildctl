"""Render the buildctl manual: docs/*.md in name order, into one HTML page.

Standard library only, so the docs build has no dependencies, like buildctl itself. The page
lands in the console package (`ui/buildctl_ui/web/docs/`), which serves it at `/docs/`, and
the same directory is published to GitHub Pages.

The Markdown is a deliberate subset: headings with `{#id}` and `signal=` attributes, paragraphs,
lists, fenced code, pipe tables (optionally preceded by a `{.wide .anchors prefix=...}` line),
inline code, bold and links.

    python3 docs/build.py           # write the page
    python3 docs/build.py --check   # exit 1 if the page is out of date
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / "ui" / "buildctl_ui" / "web" / "docs" / "index.html"
SIGNALS = {"danger": "stopped", "caution": "waiting for you", "unknown": "outcome unknown", "clear": "clear"}
CODES: dict[str, str] = {}  # stop code -> signal aspect, collected before rendering
HEADING = re.compile(r"^(#{1,4})\s+(.*?)\s*(?:\{([^}]*)\})?\s*$")
ATTRS = re.compile(r"^\{([^}]*)\}\s*$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "-", text.lower()).strip("-")


def inline(text: str) -> str:
    """Escape, then render code spans, links and bold. Code spans are protected first."""
    spans: list[str] = []

    def keep(match: re.Match[str]) -> str:
        # Each word stays whole, so a command copied mid-incident never breaks inside a flag.
        words = (match.group(1) or match.group(2)).split(" ")
        spans.append("<code>" + " ".join(f'<span class="w">{html.escape(w)}</span>' for w in words) + "</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"``\s?(.+?)\s?``|`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)
    def link(m: re.Match[str]) -> str:
        target = m.group(2)
        aspect = CODES.get(target[1:]) if target.startswith("#") else None
        glyph = signal_svg(aspect) if aspect else ""
        return f'<a href="{html.escape(target)}"{" class=\"code-link\"" if aspect else ""}>{glyph}{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def parse_attrs(raw: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in (raw or "").split():
        if token.startswith("#"):
            result["id"] = token[1:]
        elif token.startswith("."):
            result.setdefault("class", "")
            result["class"] = (result["class"] + " " + token[1:]).strip()
        elif "=" in token:
            key, value = token.split("=", 1)
            result[key] = value
    return result


def signal_svg(aspect: str) -> str:
    return (f'<svg class="sig" role="img" aria-label="{SIGNALS[aspect]}">'
            f'<use href="#sig-{aspect}"/></svg>')


def table(lines: list[str], attrs: dict[str, str]) -> str:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    head, body = rows[0], rows[2:]
    prefix = attrs.get("prefix") if "anchors" in attrs.get("class", "") else None
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead>", "<tbody>"]
    for row in body:
        row_id = ""
        if prefix is not None:
            name = re.sub(r"`", "", row[0])
            words = []
            for word in name.split():
                if word.startswith(("-", "<")):
                    break
                words.append(word)
            row_id = f' id="{prefix}{slug(" ".join(words))}"'
        cells = "".join(f"<td>{inline(c)}</td>" for c in row)
        out.append(f"<tr{row_id}>{cells}</tr>")
    out.append("</tbody></table>")
    wide = "wide" in attrs.get("class", "") or len(head) > 3
    return f'<div class="table{" wide" if wide else ""}">' + "".join(out) + "</div>"


def render(sources: list[Path]) -> tuple[str, list[tuple[str, str]], str]:
    """Return (title, chapters as (id, label), body HTML)."""
    lines: list[str] = []
    for source in sources:
        lines.extend(source.read_text().splitlines())
        lines.append("")
    entries: list[tuple[str, str, str]] = []  # (group, code, aspect) in page order
    group = ""
    for line in lines:
        heading = HEADING.match(line)
        if heading and len(heading.group(1)) == 3:
            group = heading.group(2)
        if heading and len(heading.group(1)) == 4:
            attrs = parse_attrs(heading.group(3))
            if attrs.get("signal") in SIGNALS:
                CODES[attrs.get("id", heading.group(2))] = attrs["signal"]
                entries.append((group, heading.group(2), attrs["signal"]))
    title, chapters, out = "", [], []
    open_section = open_entry = False
    pending_attrs: dict[str, str] = {}
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append(f"<p>{inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            language = line[3:].strip()
            block = []
            i += 1
            while not lines[i].startswith("```"):
                block.append(lines[i]); i += 1
            label = f' data-lang="{html.escape(language)}"' if language else ""
            out.append(f"<pre{label}><code>{html.escape(chr(10).join(block))}</code></pre>")
            i += 1
            continue
        heading = HEADING.match(line)
        if heading:
            flush()
            level, text, attrs = len(heading.group(1)), heading.group(2), parse_attrs(heading.group(3))
            ident = attrs.get("id") or slug(re.sub(r"`", "", text))
            if open_entry and level <= 4:
                out.append("</article>"); open_entry = False
            if level == 1:
                title = text
                out.append(f'<header class="title"><h1>{inline(text)}</h1>')
            elif level == 2:
                if open_section:
                    out.append("</section>")
                elif title:
                    out.append("</header>")
                chapters.append((ident, re.sub(r"`", "", text)))
                out.append(f'<section class="chapter" id="{ident}" aria-labelledby="{ident}-h">'
                           f'<h2 id="{ident}-h"><span class="num">{len(chapters)}</span>{inline(text)}</h2>')
                open_section = True
            elif level == 4 and attrs.get("signal") in SIGNALS:
                aspect = attrs["signal"]
                out.append(f'<article class="code-entry {aspect}" id="{ident}">'
                           f'<h4>{signal_svg(aspect)}<code>{html.escape(text)}</code>'
                           f'<a class="permalink" href="#{ident}" aria-label="Link to {html.escape(text)}">#</a></h4>')
                open_entry = True
            else:
                out.append(f'<h{level} id="{ident}">{inline(text)}'
                           f'<a class="permalink" href="#{ident}" aria-label="Link to this section">#</a></h{level}>')
            i += 1
            continue
        marker = ATTRS.match(line)
        if marker and marker.group(1).strip() == ".stop-index":
            flush()
            groups: dict[str, list[str]] = {}
            for name, code, aspect in entries:
                groups.setdefault(name, []).append(
                    f'<li><a href="#{code}">{signal_svg(aspect)}<code>{html.escape(code)}</code></a></li>')
            out.append('<nav class="stop-index" aria-label="Every stop code">' + "".join(
                f'<div><h3 class="index-head">{html.escape(name)}</h3><ul>{"".join(items)}</ul></div>'
                for name, items in groups.items()) + "</nav>")
            i += 1
            continue
        if marker:
            flush(); pending_attrs = parse_attrs(marker.group(1)); i += 1
            continue
        if line.startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i]); i += 1
            out.append(table(block, pending_attrs)); pending_attrs = {}
            continue
        if re.match(r"^(- |\d+\. )", line):
            flush()
            ordered = bool(re.match(r"^\d+\. ", line))
            items: list[str] = []
            while i < len(lines) and re.match(r"^(- |\d+\. |  \S)", lines[i]):
                if re.match(r"^(- |\d+\. )", lines[i]):
                    items.append(re.sub(r"^(- |\d+\. )", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{tag}>")
            continue
        if not line.strip():
            flush(); i += 1
            continue
        paragraph.append(line.strip()); i += 1
    flush()
    if open_entry:
        out.append("</article>")
    if open_section:
        out.append("</section>")
    return title, chapters, "\n".join(out)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="description" content="How to set up, run and recover a buildctl build, and what it guarantees.">
<title>{title}</title>
<link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="docs.css">
<script src="docs.js" defer></script>
</head>
<body>
<!-- Generated by docs/build.py from docs/*.md. Edit the Markdown, then run python3 docs/build.py. -->
<svg class="sprite" aria-hidden="true" focusable="false">
  <symbol id="sig-danger" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" class="g-danger"/></symbol>
  <symbol id="sig-caution" viewBox="0 0 16 16"><path d="M8 1.6 14.4 8 8 14.4 1.6 8Z" class="g-caution"/></symbol>
  <symbol id="sig-clear" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.2" class="g-clear"/></symbol>
  <symbol id="sig-unknown" viewBox="0 0 16 16"><circle cx="8" cy="8" r="4.9" class="g-unknown"/></symbol>
</svg>
<a class="skip" href="#manual">Skip to the manual</a>
<header class="bar">
  <a class="brand" href="#top"><span class="brand-word">buildctl</span><span class="brand-sub">manual</span></a>
  <nav class="chapters" aria-label="Chapters"><ol>
{nav}
  </ol></nav>
</header>
<main id="manual" tabindex="-1">
<a id="top"></a>
{body}
<footer class="foot"><p>Generated from <code>docs/*.md</code> in the buildctl repository.</p></footer>
</main>
</body>
</html>
"""


def build() -> str:
    sources = sorted(ROOT.glob("*.md"))
    title, chapters, body = render(sources)
    nav = "\n".join(f'    <li><a href="#{ident}" data-chapter="{ident}"><span class="num">{n}</span>{html.escape(label)}</a></li>'
                    for n, (ident, label) in enumerate(chapters, 1))
    return TEMPLATE.format(title=html.escape(title), nav=nav, body=body)


def main(argv: list[str]) -> int:
    page = build()
    if "--check" in argv:
        if not OUTPUT.is_file() or OUTPUT.read_text() != page:
            print(f"{OUTPUT} is out of date; run python3 docs/build.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page)
    # The page is also published on its own, so it carries its own copy of the console's icon.
    (OUTPUT.parent / "icon.svg").write_bytes((OUTPUT.parent.parent / "icon.svg").read_bytes())
    print(f"wrote {OUTPUT.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
