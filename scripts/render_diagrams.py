"""Render the Mermaid diagrams in Markdown files to PNG, so they display everywhere.

github.com renders Mermaid, but the GitHub mobile apps and many other Markdown viewers show it as
a code block. This script renders each ```mermaid block to docs/diagrams/<name>.png and rewrites
the Markdown so the image comes first, with the Mermaid source kept below it in a collapsed
<details> block. The Mermaid source stays the single source of truth: edit it, re-run this script,
and the PNG is regenerated in place.

    uv run python scripts/render_diagrams.py README.md PLAYBOOK.md

Needs the Mermaid CLI: `npm install -g @mermaid-js/mermaid-cli` (or set MMDC to its path).
Set MMDC_PUPPETEER_CONFIG to a puppeteer JSON config to use an installed Chrome.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

OUT_DIR = Path("docs/diagrams")
SUMMARY = "Diagram source (Mermaid)"
BLOCK = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<path>docs/diagrams/[^)]+\.png)\)\n\n"
    r"<details>\n<summary>[^<]*</summary>\n\n```mermaid\n(?P<src>.*?)```\n\n</details>"
    r"|```mermaid\n(?P<bare>.*?)```",
    re.DOTALL,
)
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def heading_before(text: str, position: int) -> str:
    """The nearest section heading above `position`, ignoring `#` lines inside code fences."""
    prose = re.sub(r"```.*?```", "", text[:position], flags=re.DOTALL)
    headings = HEADING.findall(prose)
    title = headings[-1] if headings else "Diagram"
    return re.sub(r"^\d+\.\s*", "", title).replace("`", "").strip()


def render(source: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as handle:
        handle.write(source)
    command = [os.environ.get("MMDC", "mmdc"), "-i", handle.name, "-o", str(target)]
    command += ["--scale", "3", "--backgroundColor", "white", "--theme", "default"]
    if config := os.environ.get("MMDC_PUPPETEER_CONFIG"):
        command += ["--puppeteerConfigFile", config]
    result = subprocess.run(command, capture_output=True, text=True)
    Path(handle.name).unlink()
    if result.returncode != 0:
        sys.exit(f"mmdc failed for {target}:\n{result.stderr}")


def process(markdown: Path) -> int:
    text = markdown.read_text(encoding="utf-8")
    counter = 0

    def replace(match: re.Match) -> str:
        nonlocal counter
        counter += 1
        source = match.group("src") or match.group("bare")
        alt = match.group("alt") or heading_before(text, match.start())
        path = (
            match.group("path")
            or (OUT_DIR / f"{markdown.stem.lower()}-{counter:02d}-{slugify(alt)}.png").as_posix()
        )
        render(source, markdown.parent / path)
        print(f"  {path}")
        return (
            f"![{alt}]({path})\n\n<details>\n<summary>{SUMMARY}</summary>\n\n"
            f"```mermaid\n{source}```\n\n</details>"
        )

    markdown.write_text(BLOCK.sub(replace, text), encoding="utf-8")
    return counter


def main() -> None:
    files = [Path(arg) for arg in sys.argv[1:]] or [Path("README.md")]
    for markdown in files:
        print(markdown)
        print(f"  {process(markdown)} diagram(s) rendered")


if __name__ == "__main__":
    main()
