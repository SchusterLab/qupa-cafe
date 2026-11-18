"""The leaderboard's figures: small multiples, two to a row, each panel a
bar for each report. Each panel ranks the reports by its own number, so
each names its rows itself. host.py gives a day's reports, one per
researcher.

matplotlib is imported only when a figure is drawn, so the rest of host.py
runs without it. Its cache goes in the service's state directory, since the
service has no home.
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# The chart's surface, ink and marks. Colour carries meaning only in the
# tokens panel, where it is the model family; every other bar is neutral.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#8a8984"
GRID = "#e4e3df"
BAR = "#6f6e69"
# Model families in a fixed order, each with its fixed colour, never by rank.
FAMILIES = (("opus", "#2a78d6"), ("sonnet", "#eb6834"), ("haiku", "#1baf7a"), ("fable", "#eda100"))
OTHER = ("other", "#b4b3ad")

# The forum shows a post's image 690 pixels wide, so a figure 6.9 inches wide
# at twice 100 dots an inch keeps its text at the size it is set in, and
# sharp. No text is smaller than 8 pt.
WIDTH_INCHES = 6.9
DPI = 200
ROW_INCHES = 0.26
COLUMNS = 2


def tokens_text(tokens: float) -> str:
    """A token count in a few characters, as the kit writes one."""
    tokens = round(tokens)
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k" if tokens < 100_000 else f"{tokens // 1000}k"
    return f"{tokens / 1_000_000:.1f}M"


def _whole(value: float) -> str:
    return f"{value:,.0f}"


def _percent(value: float) -> str:
    """A whole percent, or a tenth where rounding would make it look whole."""
    return f"{value:.0f}%" if value == round(value) or value < 99.5 else f"{value:.1f}%"


@dataclass(frozen=True)
class Panel:
    title: str
    value: Callable[[Any], float | None]
    text: Callable[[float], str] = _whole
    top: float | None = None


def _ratio(a: int | None, b: int | None, scale: float = 1.0) -> float | None:
    return None if a is None or not b else scale * a / b


USE = (
    Panel("Hours spent working", lambda r: None if r.active_minutes is None else r.active_minutes / 60,
          lambda v: f"{v:.1f}"),
    Panel("Sessions used", lambda r: r.own_sessions),
    Panel("Prompts typed", lambda r: r.own_prompts),
    Panel("Tokens used by the forum agent", lambda r: r.agent_tokens, tokens_text),
    Panel("Times the forum agent woke up", lambda r: r.agent_wakes),
)

WORKFLOW = (
    Panel("Tool calls", lambda r: r.tool_calls),
    Panel("Tool calls per prompt", lambda r: _ratio(r.tool_calls, r.own_prompts)),
    Panel("Most tool calls without a prompt", lambda r: r.longest_run),
    Panel("Subagents launched", lambda r: r.subagents),
    Panel("Web searches and page fetches", lambda r: r.web_calls),
    Panel("Browser and computer-use actions", lambda r: r.browser_calls),
    Panel("Most sessions running at once", lambda r: r.peak_sessions),
    Panel("% of calls needing no approval", lambda r: _ratio(r.unattended_calls, r.model_calls, 100),
          _percent, top=100),
)


LENDING = (
    Panel("Tasks their agent answered for others", lambda r: r.lent_tasks),
    Panel("Tokens their agent spent for others", lambda r: r.lent_tokens, tokens_text),
    Panel("Tasks lent agents answered for them", lambda r: r.borrowed_tasks),
    Panel("Tokens lent agents spent for them", lambda r: r.borrowed_tokens, tokens_text),
)


def _families(models: dict[str, int] | None, total: int) -> list[tuple[str, str, int]]:
    """(family, colour, tokens) in the fixed order, the rest as "other"."""
    if not models:
        return [(*OTHER, total)] if total else []
    known = dict(FAMILIES)
    found = [(name, colour, models[name]) for name, colour in FAMILIES if models.get(name)]
    rest = sum(v for k, v in models.items() if k not in known)
    return found + ([(*OTHER, rest)] if rest else [])


def _pyplot():
    os.environ.setdefault(
        "MPLCONFIGDIR", str(Path(os.environ.get("STATE_DIRECTORY") or tempfile.gettempdir()) / "matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def ranked(names: list[str], values: list[float | None]) -> list[tuple[str, float | None]]:
    """(label, value) from the largest value down, the label "place. name".
    Equal values share a place, and a missing value comes last, unplaced."""
    order = sorted(range(len(values)), key=lambda i: (values[i] is None, -(values[i] or 0)))
    rows, place = [], 0
    for at, i in enumerate(order):
        if values[i] is None:
            rows.append((names[i], None))
            continue
        if not at or values[i] != values[order[at - 1]]:
            place = at + 1
        rows.append((f"{place}. {names[i]}", values[i]))
    return rows


def _style(ax, labels: list[str]) -> None:
    from matplotlib.ticker import MaxNLocator

    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(axis="x", colors=INK_MUTED, labelsize=8, length=0)
    ax.tick_params(axis="y", length=0, labelsize=8.5, colors=INK)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.xaxis.set_major_locator(MaxNLocator(3, integer=True))
    ax.set_axisbelow(True)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.set_ylim(len(labels) - 0.5, -0.5)


def _value_labels(ax, values: list[float | None], text: Callable[[float], str], top: float) -> None:
    for row, value in enumerate(values):
        if value is None:
            ax.text(0, row, "n/a", va="center", ha="left", fontsize=8, color=INK_MUTED)
        else:
            ax.text(value + top * 0.02, row, text(value), va="center", ha="left", fontsize=8,
                    color=INK_SECONDARY)


def _figure(plt, title: str, count: int, cells: int):
    rows = -(-cells // COLUMNS)
    height = 0.3 + rows * (0.45 + ROW_INCHES * max(count, 2))
    fig, axes = plt.subplots(rows, COLUMNS, figsize=(WIDTH_INCHES, height), dpi=DPI, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK, fontweight="bold")
    return fig, [ax for row in axes for ax in row]


def _layout(fig) -> None:
    """The panels under the title, which takes its first 0.3 inches."""
    fig.tight_layout(rect=(0, 0, 1, 1 - 0.3 / fig.get_figheight()), h_pad=1.2, w_pad=1.0)


def _png(fig, plt) -> bytes:
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    plt.close(fig)
    return out.getvalue()


def _bars(ax, panel: Panel, names: list[str], reports: list[Any]) -> None:
    labels, values = zip(*ranked(names, [panel.value(r) for r in reports]))
    _style(ax, list(labels))
    top = panel.top or max([v for v in values if v is not None] + [1])
    ax.barh(range(len(values)), [v or 0 for v in values], height=0.62, color=BAR)
    ax.set_xlim(0, top * 1.38)
    if panel.top:
        ax.set_xticks([0, panel.top / 2, panel.top])
    _value_labels(ax, values, panel.text, top)
    ax.set_title(panel.title, loc="left", fontsize=9, color=INK, pad=4)


def figures(names: list[str], reports: list[Any], use_title: str, workflow_title: str) -> list[tuple[str, bytes]]:
    """Two PNGs, as (alt text, bytes): how much was used, and how. Each
    panel has a row for each report, named from ``names`` and ranked by the
    panel's number. A report from a kit that does not count a number shows
    "n/a" for it, last."""
    plt = _pyplot()
    made = []

    fig, axes = _figure(plt, use_title, len(reports), 1 + len(USE))
    ax = axes[0]
    rows = ranked(names, [float(r.own_tokens) for r in reports])
    order = sorted(range(len(reports)), key=lambda i: -reports[i].own_tokens)
    _style(ax, [label for label, _ in rows])
    tops = []
    seen: dict[str, str] = {}
    for row, report in enumerate(reports[i] for i in order):
        left = 0
        for name, colour, tokens in _families(getattr(report, "models", None), report.own_tokens):
            ax.barh(row, tokens, left=left, height=0.62, color=colour, edgecolor=SURFACE, linewidth=0.8)
            left += tokens
            seen[name] = colour
        tops.append(report.own_tokens)
    top = max(tops + [1])
    ax.set_xlim(0, top * 1.38)
    _value_labels(ax, [float(t) for t in tops], tokens_text, top)
    ax.set_title("Tokens used, by model", loc="left", fontsize=9, color=INK, pad=4)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: tokens_text(v) if v else "0"))
    if seen:
        order = [name for name, _ in (*FAMILIES, OTHER) if name in seen]
        handles = [plt.Rectangle((0, 0), 1, 1, color=seen[name]) for name in order]
        fig.legend(handles, order, loc="upper right", ncol=len(order), frameon=False, fontsize=8.5,
                   labelcolor=INK_SECONDARY, handlelength=1, handleheight=1, bbox_to_anchor=(0.99, 1.0))
    for ax, panel in zip(axes[1:], USE):
        _bars(ax, panel, names, reports)
        if panel.text is tokens_text:
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: tokens_text(v) if v else "0"))
    for ax in axes[1 + len(USE):]:
        ax.set_visible(False)
    _layout(fig)
    made.append((use_title, _png(fig, plt)))

    fig, axes = _figure(plt, workflow_title, len(reports), len(WORKFLOW))
    for ax, panel in zip(axes, WORKFLOW):
        _bars(ax, panel, names, reports)
    for ax in axes[len(WORKFLOW):]:
        ax.set_visible(False)
    _layout(fig)
    made.append((workflow_title, _png(fig, plt)))
    return made


def lending_figure(names: list[str], rows: list[Any], title: str) -> bytes:
    """A PNG of how agents were lent: for each researcher in ``rows``, what
    their agent did for others and what agents lent to them did for them."""
    plt = _pyplot()
    fig, axes = _figure(plt, title, len(rows), len(LENDING))
    for ax, panel in zip(axes, LENDING):
        _bars(ax, panel, names, rows)
        if panel.text is tokens_text:
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: tokens_text(v) if v else "0"))
    _layout(fig)
    return _png(fig, plt)
