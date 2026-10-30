"""Tests for arxiv-feed.py.

The parser against an abridged copy of the real feed, and a run against a stub
forum over real HTTP that holds external ids unique the way Discourse does.

Run from the repository root::

    python3 -m pytest server/tests -q
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "quant-ph.xml"
TEMPLATE = (HERE.parent.parent / "content" / "arxiv-topic.md").read_text(encoding="utf-8")
DETAILS = (HERE.parent.parent / "content" / "arxiv-details.md").read_text(encoding="utf-8")

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


def test_the_links_follow_the_authors_on_one_line():
    assert feed.topic_body(papers()[1], TEMPLATE) == (
        "Jane Doe. [Abstract](https://arxiv.org/abs/2610.00112), [PDF](https://arxiv.org/pdf/2610.00112)."
    )


def test_a_long_author_list_is_cut_to_fit_the_reply():
    paper = papers()[0]
    many = feed.Paper(**{**paper.__dict__, "authors": [f"Author {n}" for n in range(300)]})
    affs = [[f"Department of Physics, University of Place {n}, Somewhere 12345, Country"] for n in range(300)]

    body = feed.details_body(many, DETAILS, affs)

    assert len(body) <= feed.MAX_POST_LENGTH
    assert "Department of Physics, University of Place 0, Somewhere 12345, Country" in body
    assert re.search(r"and \d+ more", body)
    assert body.endswith(paper.abstract)


def test_text_that_looks_like_a_placeholder_is_copied():
    paper = papers()[0]
    tricky = feed.Paper(**{**paper.__dict__, "abstract": "We bound %PDF_URL% and 50% of %ABS_URL%."})

    assert "We bound %PDF_URL% and 50% of %ABS_URL%." in feed.details_body(tricky, DETAILS, None)


def test_no_placeholder_is_left_unfilled():
    for paper in papers():
        for body in (feed.topic_body(paper, TEMPLATE), feed.details_body(paper, DETAILS, None)):
            assert "%" not in body.replace("50%", "")


# ---------------------------------------------------------- affiliations

# The top of an arXiv HTML page as LaTeXML writes it, one author at a time.
PER_AUTHOR = """<html><body><div class="ltx_page_main"><h1 class="ltx_title">Global and nonlocal magic</h1>
<div class="ltx_authors">
<span class="ltx_creator ltx_role_author"><span class="ltx_personname">Lukas Ebner</span><span class="ltx_note ltx_role_thanks"><span class="ltx_note_content">thanks: lukas@example.org</span></span><span class="ltx_author_notes"><span class="ltx_author_notes_content">
<span class="ltx_contact ltx_role_email"><span class="ltx_contact_name">Email: </span><a href="mailto:lukas@example.org">lukas@example.org</a></span>
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span>Department of Physics, <br class="ltx_break">University of Ottawa, Ontario, K1N 6N5, Canada </span></span></span></span>
<span class="ltx_author_before"> </span><span class="ltx_creator ltx_role_author"><span class="ltx_personname">Roberto Fernández-Barrios</span><span class="ltx_author_notes"><span class="ltx_author_notes_content">
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span>Current affiliation: Sparrow Quantum, Nordre Fasanvej 215, DK-2000 Frederiksberg, Denmark </span>
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span>Institute for <math alttext="\\mathbb{Z}"><mi>Z</mi></math> Physics, Leibniz Universität Hannover, Hannover, Germany. </span></span></span></span>
<span class="ltx_author_before"> </span><span class="ltx_creator ltx_role_author"><span class="ltx_personname">Mercedes Martín-Benito</span><span class="ltx_author_notes"><span class="ltx_author_notes_content">
<span class="ltx_contact ltx_role_address"><span class="ltx_contact_name">Address: </span>Department of Physics, Stanford University, Stanford, CA 94305, USA </span></span></span></span>
</div>
<div class="ltx_abstract"><p>Here, using global ...</p></div>
<div class="ltx_authors"><span class="ltx_creator ltx_role_author"><span class="ltx_personname">Not an author</span></span></div>
"""

# One name block with numbers, and affiliations that start with theirs (authblk).
NUMBERED = """<div class="ltx_authors"><span class="ltx_creator ltx_role_author"><span class="ltx_personname">Lukas Ebner<sup class="ltx_sup">1</sup><sup class="ltx_sup">2</sup>,
Roberto Fernández-Barrios<sup class="ltx_sup">2,*</sup>, and
Mercedes Martín-Benito<sup class="ltx_sup">3</sup>
</span><span class="ltx_author_notes"><span class="ltx_author_notes_content">
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span><sup class="ltx_sup">1</sup>Tsinghua University
</span>
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span><sup class="ltx_sup">2</sup>Zhongguancun Laboratory
</span>
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span><sup class="ltx_sup">3</sup>Institute of Software, Chinese Academy of Sciences
</span>
<span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">Affiliation: </span>
lukas@example.org;
roberto@example.org
</span></span></span></span></div>"""


def test_each_author_has_the_affiliations_the_html_gives_them():
    assert feed.affiliations(papers()[0], PER_AUTHOR) == [
        ["Department of Physics, University of Ottawa, Ontario, K1N 6N5, Canada"],
        ["Current affiliation: Sparrow Quantum, Nordre Fasanvej 215, DK-2000 Frederiksberg, Denmark",
         "Institute for Physics, Leibniz Universität Hannover, Hannover, Germany"],
        ["Department of Physics, Stanford University, Stanford, CA 94305, USA"],
    ]


def test_numbered_affiliations_go_to_the_authors_whose_numbers_they_carry():
    assert feed.affiliations(papers()[0], NUMBERED) == [
        ["Tsinghua University", "Zhongguancun Laboratory"],
        ["Zhongguancun Laboratory"],
        ["Institute of Software, Chinese Academy of Sciences"],
    ]


def test_one_unnumbered_name_block_shares_its_affiliations():
    html = NUMBERED.split("<sup")[0] + ", Roberto Fernández-Barrios and Mercedes Martín-Benito</span>" \
        '<span class="ltx_contact ltx_role_affiliation">Simula UiB, N-5006 Bergen, Norway Emails: {lukas, roberto}@simula.no</span></span></div>'

    assert feed.affiliations(papers()[0], html) == [["Simula UiB, N-5006 Bergen, Norway"]] * 3


def test_a_page_whose_authors_cannot_be_matched_gives_no_affiliations():
    two = PER_AUTHOR.split('<span class="ltx_author_before"> </span>', 2)
    html = two[0] + "</div>"

    assert feed.affiliations(papers()[0], html) is None
    assert feed.affiliations(papers()[0], "<html><body>No authors here</body></html>") is None
    assert feed.affiliations(papers()[0], None) is None


def test_the_affiliations_arxiv_lists_with_the_names_are_used_when_the_html_gives_none():
    creators = feed.split_creators("Ian N. Hammock (Pritzker School, University of Chicago, Chicago, USA), Kinfung Ngan, "
                                   "Nazar Delegan (Argonne National Laboratory (ANL), Lemont)")
    assert creators == [
        ("Ian N. Hammock", "Pritzker School, University of Chicago, Chicago, USA"),
        ("Kinfung Ngan", ""),
        ("Nazar Delegan", "Argonne National Laboratory (ANL), Lemont"),
    ]
    paper = feed.Paper(**{**papers()[0].__dict__, "authors": [n for n, _ in creators],
                          "listed_affiliations": [a for _, a in creators]})

    assert feed.affiliations(paper, None) == [["Pritzker School, University of Chicago, Chicago, USA"], [],
                                              ["Argonne National Laboratory (ANL), Lemont"]]


def test_a_footnote_among_the_affiliations_is_no_affiliation():
    html = PER_AUTHOR.replace("Department of Physics, <br class=\"ltx_break\">University of Ottawa",
                              "These two authors contributed equally to this work</span>"
                              '<span class="ltx_contact ltx_role_affiliation">University of Ottawa')
    html = html.replace("Stanford, CA 94305, USA", "Stanford, CA 94305, USACorresponding author")

    affs = feed.affiliations(papers()[0], html)

    assert affs[0] == ["University of Ottawa, Ontario, K1N 6N5, Canada"]
    assert affs[2] == ["Department of Physics, Stanford University, Stanford, CA 94305, USA"]


# ----------------------------------------------------------------- the posts


def test_the_topic_names_the_first_and_last_authors_and_no_affiliations():
    paper = papers()[0]
    two = feed.Paper(**{**paper.__dict__, "authors": paper.authors[:2]})
    many = feed.Paper(**{**paper.__dict__, "authors": [*paper.authors, "Ada Lovelace"]})

    assert feed.topic_body(paper, TEMPLATE).startswith("Lukas Ebner, 1 other author, and Mercedes Martín-Benito. [Abstract](")
    assert feed.topic_body(many, TEMPLATE).startswith("Lukas Ebner, 2 other authors, and Ada Lovelace. [Abstract](")
    assert feed.topic_body(two, TEMPLATE).startswith("Lukas Ebner and Roberto Fernández-Barrios. [Abstract](")
    assert feed.topic_body(papers()[1], TEMPLATE).startswith("Jane Doe. [Abstract](")


def test_the_reply_lists_every_author_with_numbered_affiliations_in_full_and_the_abstract():
    paper = papers()[0]
    body = feed.details_body(paper, DETAILS, feed.affiliations(paper, PER_AUTHOR))

    assert body.startswith(
        "**Authors:** Lukas Ebner¹, Roberto Fernández-Barrios² ³, Mercedes Martín-Benito⁴\n\n"
        "¹Department of Physics, University of Ottawa, Ontario, K1N 6N5, Canada\n"
        "²Current affiliation: Sparrow Quantum, Nordre Fasanvej 215, DK-2000 Frederiksberg, Denmark\n"
        "³Institute for Physics, Leibniz Universität Hannover, Hannover, Germany\n"
        "⁴Department of Physics, Stanford University, Stanford, CA 94305, USA\n\n"
    )
    assert body.endswith(f"**Abstract:** {paper.abstract}")


def test_authors_who_share_their_affiliations_need_no_numbers():
    paper = papers()[0]
    bergen, simula = "Department of Physics, University of Bergen, Norway", "Simula UiB, Bergen, Norway"

    assert feed.details_body(paper, DETAILS, [[bergen], [simula], [bergen, bergen]]).startswith(
        f"**Authors:** Lukas Ebner¹, Roberto Fernández-Barrios², Mercedes Martín-Benito¹\n\n¹{bergen}\n²{simula}\n\n"
    )
    assert feed.details_body(paper, DETAILS, [[bergen]] * 3).startswith(
        f"**Authors:** Lukas Ebner, Roberto Fernández-Barrios, Mercedes Martín-Benito\n\n{bergen}\n\n"
    )
    assert feed.details_body(paper, DETAILS, None).startswith(
        "**Authors:** Lukas Ebner, Roberto Fernández-Barrios, Mercedes Martín-Benito\n\n**Abstract:**"
    )


# ------------------------------------------------------------------ a run


class StubForum(BaseHTTPRequestHandler):
    """The Discourse endpoints the poster uses, with external ids unique, and
    arXiv's HTML versions."""

    topics: dict[str, dict] = {}
    refuse: set[str] = set()
    # The category's page, two topics to a page, and what was deleted from it.
    listed: list[dict] = []
    deleted: list[int] = []
    refuse_delete: set[int] = set()
    forbid_lists = False
    # Topic ids whose replies are refused as the forum being down.
    down_for_replies: set[int] = set()
    # arXiv id -> HTML page; any other id is a 404.
    html: dict[str, str] = {}

    def log_message(self, *args):  # noqa: D401 - silence the test output
        pass

    def reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def by_id(self, topic_id: int) -> dict:
        return next(t for t in self.topics.values() if t["id"] == topic_id)

    def do_GET(self):
        if self.path.startswith("/html/"):
            page = self.html.get(self.path.removeprefix("/html/"))
            if page is None:
                self.reply(404, {"error": "No HTML for this paper"})
                return
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/c/7/l/latest.json"):
            if self.forbid_lists:
                self.reply(403, {"error_type": "invalid_access"})
                return
            page = int(self.path.partition("page=")[2] or 0)
            more = len(self.listed) > 2 * (page + 1)
            body = {"topics": self.listed[2 * page: 2 * page + 2]}
            if more:
                body["more_topics_url"] = f"/c/7/l/latest?page={page + 1}"
            users = [{"id": n, "username": name} for name, n in POSTER_IDS.items()]
            self.reply(200, {"users": users, "topic_list": body})
            return
        external_id = urllib.parse.unquote(self.path.removeprefix("/t/external_id/").removesuffix(".json"))
        if external_id in self.topics:
            topic = self.topics[external_id]
            posts = [{"post_number": 1, "username": "arxiv-bot"}] + [
                {"post_number": n + 2, "username": "arxiv-bot"} for n, _ in enumerate(topic["replies"])
            ]
            self.reply(200, {"id": topic["id"], "post_stream": {"posts": posts}})
        else:
            self.reply(404, {"errors": ["not found"]})

    def do_DELETE(self):
        topic_id = int(self.path.removeprefix("/t/").removesuffix(".json"))
        if topic_id in self.refuse_delete:
            self.reply(403, {"error_type": "invalid_access"})
        else:
            self.deleted.append(topic_id)
            self.reply(200, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        if "topic_id" in form:
            topic_id = int(form["topic_id"])
            if topic_id in self.down_for_replies:
                self.reply(502, {"errors": ["Bad gateway"]})
                return
            self.by_id(topic_id)["replies"].append(form["raw"])
            self.reply(200, {"topic_id": topic_id, "post_number": 2})
        elif form["external_id"] in self.topics:
            self.reply(422, {"errors": ["External ID has already been taken"]})
        elif form["external_id"] in self.refuse:
            self.reply(422, {"errors": ["Title seems unclear"]})
        else:
            topic_id = len(self.topics) + 100
            self.topics[form["external_id"]] = {**form, "id": topic_id, "replies": []}
            self.reply(200, {"topic_id": topic_id, "post_number": 1})


@pytest.fixture
def forum(monkeypatch, tmp_path):
    StubForum.topics = {}
    StubForum.refuse = set()
    StubForum.listed = []
    StubForum.deleted = []
    StubForum.refuse_delete = set()
    StubForum.forbid_lists = False
    StubForum.down_for_replies = set()
    StubForum.html = {"2610.00129": PER_AUTHOR, "2610.00112": "<html><body>A page with no author list</body></html>"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubForum)
    # Polled often, so the shutdown at teardown is not half a second.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(feed, "FORUM_URL", url)
    monkeypatch.setattr(feed, "HTML_URL", url + "/html/")
    monkeypatch.setattr(feed, "API_KEY", "key")
    monkeypatch.setattr(feed, "CATEGORY_ID", "7")
    monkeypatch.setattr(feed, "POST_INTERVAL", 0)
    monkeypatch.setattr(feed, "HTML_INTERVAL", 0)
    monkeypatch.setattr(feed, "STATE_PATH", tmp_path / "state.json")
    yield StubForum
    server.shutdown()


POSTER_IDS = {"system": -1, "arxiv-bot": 5, "sunada-agent": 9}


def listed(topic_id: int, title: str, posts_count: int = 1, pinned: bool = False,
           last_poster: str = "arxiv-bot", opener: str = "arxiv-bot") -> dict:
    """A topic as the category's page lists it."""
    posters = [{"description": "Original Poster", "user_id": POSTER_IDS[opener]}]
    return {"id": topic_id, "title": title, "posts_count": posts_count, "pinned": pinned,
            "last_poster_username": last_poster, "posters": posters}


def test_a_run_opens_a_topic_per_paper_and_replies_with_the_details(forum):
    assert feed.run(str(FIXTURE), dry_run=False) == 0

    assert set(forum.topics) == {"arxiv-2610_00129", "arxiv-2610_00112"}
    topic = forum.topics["arxiv-2610_00129"]
    assert topic["category"] == "7"
    assert topic["title"] == "Global and nonlocal magic of quantum many-body scars (arXiv:2610.00129)"
    assert topic["raw"].startswith("Lukas Ebner, 1 other author, and Mercedes Martín-Benito. [Abstract](")
    (details,) = topic["replies"]
    assert "Roberto Fernández-Barrios² ³" in details and details.endswith(papers()[0].abstract)


def test_a_page_with_no_author_list_is_posted_without_waiting(forum):
    feed.run(str(FIXTURE), dry_run=False)

    topic = forum.topics["arxiv-2610_00112"]
    assert topic["raw"].startswith("Jane Doe. [Abstract](")
    assert topic["replies"][0].startswith("**Authors:** Jane Doe\n\n**Abstract:**")


def test_a_paper_waits_for_its_html_version_and_then_goes_without(forum, monkeypatch):
    del forum.html["2610.00112"]

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert set(forum.topics) == {"arxiv-2610_00129"}
    assert [e["paper"]["arxiv_id"] for e in json.loads(feed.STATE_PATH.read_text())["queue"]] == ["2610.00112"]

    # The same build again, as a run that finds the feed unchanged.
    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert set(forum.topics) == {"arxiv-2610_00129"}

    monkeypatch.setattr(feed, "HTML_WAIT", 0)
    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert forum.topics["arxiv-2610_00112"]["raw"].startswith("Jane Doe. [Abstract](")
    assert json.loads(feed.STATE_PATH.read_text())["queue"] == []


def test_a_paper_whose_html_turns_up_is_posted_with_it(forum):
    page = forum.html.pop("2610.00129")
    feed.run(str(FIXTURE), dry_run=False)
    assert "arxiv-2610_00129" not in forum.topics

    forum.html["2610.00129"] = page
    feed.run(str(FIXTURE), dry_run=False)
    assert "Stanford University, Stanford, CA 94305" in forum.topics["arxiv-2610_00129"]["replies"][0]


def test_a_second_run_of_the_same_build_posts_and_deletes_nothing(forum):
    feed.run(str(FIXTURE), dry_run=False)
    forum.topics.clear()
    forum.listed = [listed(2, "Unanswered (arXiv:2609.00001)")]

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert forum.topics == {}
    assert forum.deleted == []


def test_a_build_posted_before_the_queue_is_not_posted_again(forum):
    build, _ = feed.parse_feed(FIXTURE.read_text(encoding="utf-8"))
    feed.save_state(feed.STATE_PATH, {"posted_build": build})

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert forum.topics == {}


def test_a_run_that_lost_its_state_is_refused_rather_than_duplicating(forum):
    feed.run(str(FIXTURE), dry_run=False)
    feed.STATE_PATH.unlink()

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert len(forum.topics) == 2
    assert all(len(topic["replies"]) == 1 for topic in forum.topics.values())


def test_a_reply_the_forum_could_not_take_is_posted_by_the_next_run(forum):
    forum.down_for_replies = {100}

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert forum.topics["arxiv-2610_00129"]["replies"] == []

    forum.down_for_replies = set()
    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert len(forum.topics["arxiv-2610_00129"]["replies"]) == 1
    assert len(forum.topics["arxiv-2610_00112"]["replies"]) == 1


def test_a_refused_paper_does_not_hold_back_the_rest_and_fails_the_unit(forum):
    forum.refuse = {"arxiv-2610_00129"}

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert set(forum.topics) == {"arxiv-2610_00112"}


def test_a_forum_that_is_down_leaves_the_build_to_be_retried(monkeypatch, tmp_path):
    def refused(*args, **kwargs):
        # As urlopen fails on a closed port, without Windows' two seconds of retries.
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(feed.urllib.request, "urlopen", refused)
    monkeypatch.setattr(feed, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(feed, "POST_INTERVAL", 0)
    monkeypatch.setattr(feed, "HTML_INTERVAL", 0)

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert not (tmp_path / "state.json").exists()


def test_a_dry_run_posts_nothing_and_keeps_no_state(forum, capsys):
    del forum.html["2610.00112"]

    assert feed.run(str(FIXTURE), dry_run=True) == 0

    assert forum.topics == {}
    assert not feed.STATE_PATH.exists()
    out = capsys.readouterr().out
    assert "arxiv-2610_00112" in out and "--- reply\n**Authors:** Jane Doe" in out


# ------------------------------------------------------------------ unreplied topics


def test_unreplied_preprints_are_deleted_before_a_build_is_posted(forum):
    forum.listed = [
        listed(1, "About the arXiv quant-ph category", opener="system"),
        listed(2, "Old and unanswered (arXiv:2609.00001)"),
        listed(3, "Old and answered (arXiv:2609.00002)", posts_count=3),
        listed(4, "Pinned by staff (arXiv:2609.00003)", pinned=True),
        listed(5, "Only the feed's reply (arXiv:2609.00004)", posts_count=2),
        listed(6, "Global and nonlocal magic of quantum many-body scars (arXiv:2610.00129)"),
        listed(7, "Answered once (arXiv:2609.00005)", posts_count=2, last_poster="sunada-agent"),
        listed(8, "Opened by an agent (arXiv:2609.00006)", opener="sunada-agent", last_poster="sunada-agent"),
    ]

    assert feed.run(str(FIXTURE), dry_run=False) == 0
    assert forum.deleted == [2, 5]
    assert set(forum.topics) == {"arxiv-2610_00129", "arxiv-2610_00112"}


def test_a_refused_deletion_still_posts_the_build_and_fails_the_unit(forum):
    forum.listed = [listed(2, "Kept (arXiv:2609.00001)"), listed(5, "Gone (arXiv:2609.00004)")]
    forum.refuse_delete = {2}

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert forum.deleted == [5]
    assert len(forum.topics) == 2


def test_a_key_that_cannot_list_the_category_still_posts_the_build(forum):
    forum.forbid_lists = True

    assert feed.run(str(FIXTURE), dry_run=False) == 1
    assert len(forum.topics) == 2


def test_a_dry_run_with_a_key_names_what_it_would_delete(forum, capsys):
    forum.listed = [listed(2, "Unanswered (arXiv:2609.00001)")]

    assert feed.run(str(FIXTURE), dry_run=True) == 0
    assert forum.deleted == []
    assert "would delete topic 2" in capsys.readouterr().out
