"""Tests for arxiv-feed.py.

The parser against an abridged copy of the real feed, and a run against a stub
forum over real HTTP that holds external ids unique the way Discourse does.

Run from the repository root::

    python3 -m pytest server/tests -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "quant-ph.xml"
TEMPLATE = (HERE.parent.parent / "content" / "arxiv-topic.md").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("arxiv_feed", HERE.parent / "arxiv-feed.py")
feed = importlib.util.module_from_spec(spec)
# Registered before it runs, because a dataclass looks its module up by name.
sys.modules["arxiv_feed"] = feed
spec.loader.exec_module(feed)


def papers():
    return feed.parse_feed(FIXTURE.read_text(encoding="utf-8"))[1]


# ---------------------------------------------------------------- the parser


def test_only_new_submissions_and_cross_lists_are_kept():
    assert [(p.arxiv_id, p.announce_type) for p in papers()] == [
        ("2610.00129", "new"),
        ("2610.00112", "cross"),
    ]


def test_the_build_stamp_is_read():
    build, _ = feed.parse_feed(FIXTURE.read_text(encoding="utf-8"))
    assert build == "Fri, 02 Oct 2026 04:00:01 +0000"


def test_the_abstract_is_one_paragraph_without_the_preamble():
    """A newline inside it would render as a line break on the forum."""
    abstract = papers()[0].abstract
    assert abstract.startswith("Here, using global")
    assert "\n" not in abstract
    assert "Announce Type" not in abstract


def test_tex_in_the_abstract_is_left_for_the_forum_to_render():
    assert r"$\mathbb{Z}_2$" in papers()[0].abstract


def test_tex_accents_in_names_become_letters():
    assert papers()[0].authors == ["Lukas Ebner", "Roberto Fernández-Barrios", "Mercedes Martín-Benito"]


@pytest.mark.parametrize(
    "tex, text",
    [
        (r"G\"odel", "Gödel"),
        (r"{\"o}", "ö"),
        (r"\'{e}", "é"),
        (r"Dvo\v{r}\'ak", "Dvořák"),
        (r"\c{C}elik", "Çelik"),
        (r"Stra\ss e", "Straße"),
        (r"\o{}stergaard", "østergaard"),
        (r"\L{}ukasz", "Łukasz"),
        ("Plain Name", "Plain Name"),
    ],
)
def test_detex(tex, text):
    assert feed.detex(tex) == text


def test_the_primary_category_is_the_first_listed():
    assert papers()[1].primary_category == "math.PR"


# ----------------------------------------------------------------- the topic


def test_the_external_id_is_one_discourse_accepts():
    """Discourse allows [\\w-] and at most 50 characters; a dot is refused."""
    external_id = papers()[0].external_id
    assert external_id == "arxiv-2610_00129"
    assert len(external_id) <= 50


def test_the_title_carries_the_id():
    assert feed.topic_title(papers()[0]) == "Global and nonlocal magic of quantum many-body scars (arXiv:2610.00129)"


def test_a_long_title_is_cut_to_fit_and_keeps_the_id():
    paper = papers()[0]
    long = feed.Paper(**{**paper.__dict__, "title": "word " * 100})

    title = feed.topic_title(long)

    assert len(title) <= feed.MAX_TITLE_LENGTH
    assert title.endswith("(arXiv:2610.00129)")


def test_the_body_says_where_a_paper_came_from():
    new, cross = papers()

    assert "Announced in quant-ph on 2 October 2026" in feed.topic_body(new, TEMPLATE)
    assert "Cross-listed to quant-ph from math.PR on 2 October 2026" in feed.topic_body(cross, TEMPLATE)
    assert "[PDF](https://arxiv.org/pdf/2610.00112)" in feed.topic_body(cross, TEMPLATE)


def test_a_long_author_list_is_cut():
    paper = papers()[0]
    many = feed.Paper(**{**paper.__dict__, "authors": [f"Author {n}" for n in range(300)]})

    body = feed.topic_body(many, TEMPLATE)

    assert "Author 24," in body
    assert "Author 25" not in body
    assert "and 275 more" in body


def test_text_that_looks_like_a_placeholder_is_copied():
    paper = papers()[0]
    tricky = feed.Paper(**{**paper.__dict__, "abstract": "We bound %PDF_URL% and 50% of %ABS_URL%."})

    assert "We bound %PDF_URL% and 50% of %ABS_URL%." in feed.topic_body(tricky, TEMPLATE)


def test_no_placeholder_is_left_unfilled():
    for paper in papers():
        assert "%" not in feed.topic_body(paper, TEMPLATE).replace("50%", "")


# ------------------------------------------------------------------ a run


class StubForum(BaseHTTPRequestHandler):
    """Discourse's two endpoints the poster uses, with external ids unique."""

    topics: dict[str, dict] = {}
    refuse: set[str] = set()

    def log_message(self, *args):  # noqa: D401 - silence the test output
        pass

    def reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        external_id = urllib.parse.unquote(self.path.removeprefix("/t/external_id/").removesuffix(".json"))
        if external_id in self.topics:
            self.reply(200, {"id": 1})
        else:
            self.reply(404, {"errors": ["not found"]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        if form["external_id"] in self.topics:
            self.reply(422, {"errors": ["External ID has already been taken"]})
        elif form["external_id"] in self.refuse:
            self.reply(422, {"errors": ["Title seems unclear"]})
        else:
            self.topics[form["external_id"]] = form
            self.reply(200, {"id": len(self.topics)})


@pytest.fixture
def forum(monkeypatch, tmp_path):
    StubForum.topics = {}
    StubForum.refuse = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubForum)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr(feed, "FORUM_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(feed, "API_KEY", "key")
    monkeypatch.setattr(feed, "CATEGORY_ID", "7")
    monkeypatch.setattr(feed, "POST_INTERVAL", 0)
    monkeypatch.setattr(feed, "STATE_PATH", tmp_path / "state.json")
    yield StubForum
    server.shutdown()


def test_a_run_opens_a_topic_per_paper(forum):
    assert feed.run(str(FIXTURE), dry_run=False) == 0

    assert set(forum.topics) == {"arxiv-2610_00129", "arxiv-2610_00112"}
    topic = forum.topics["arxiv-2610_00129"]
    assert topic["category"] == "7"
    assert topic["title"].endswith("(arXiv:2610.00129)")


def test_a_second_run_of_the_same_build_posts_nothing(forum):
    feed.run(str(FIXTURE), dry_run=False)
    forum.topics.clear()

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert forum.topics == {}


def test_a_run_that_lost_its_state_is_refused_rather_than_duplicating(forum):
    feed.run(str(FIXTURE), dry_run=False)
    feed.STATE_PATH.unlink()

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert len(forum.topics) == 2


def test_a_refused_paper_does_not_hold_back_the_rest_and_fails_the_unit(forum):
    forum.refuse = {"arxiv-2610_00129"}

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert set(forum.topics) == {"arxiv-2610_00112"}


def test_a_forum_that_is_down_leaves_the_build_to_be_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(feed, "FORUM_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(feed, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(feed, "POST_INTERVAL", 0)

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert not (tmp_path / "state.json").exists()


def test_a_dry_run_posts_nothing_and_keeps_no_state(forum, capsys):
    assert feed.run(str(FIXTURE), dry_run=True) == 0

    assert forum.topics == {}
    assert not feed.STATE_PATH.exists()
    assert "arxiv-2610_00112" in capsys.readouterr().out
