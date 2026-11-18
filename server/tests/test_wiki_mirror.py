"""The lab wiki's mirror: Parsoid's HTML as Markdown, a page cut into posts,
and the topic kept in step with the page.

Run from the repository root::

    python3 -m pytest server/tests -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("wiki_mirror", HERE.parent / "wiki-mirror.py")
wm = importlib.util.module_from_spec(spec)
sys.modules["wiki_mirror"] = wm
spec.loader.exec_module(wm)


def page(body: str) -> str:
    return f'<html><head><title>x</title><meta charset="utf-8"/></head><body>{body}</body></html>'


def convert(body: str) -> list[str]:
    conv = wm.Converter(lambda title, fragment: f"/t/external_id/labwiki-{title}",
                        lambda src, alt, w, h: f"![{alt}|{w}x{h}](upload://{src.rsplit('/', 1)[-1]})")
    return [b.text for b in conv.blocks(page(body))]


# ---------------------------------------------------------------- converting


def test_headings_paragraphs_and_inline_marks():
    blocks = convert('<section><span typeof="mw:Transclusion" data-mw="{}"></span>'
                     '<h2 id="Cool_down">Cool down</h2><p>Do this <b>before</b> cooling, <i>gently</i>, '
                     'with <code>V14</code>.<br/>Then wait.</p></section>')
    assert blocks == ["## Cool down", "Do this **before** cooling, *gently*, with `V14`.  \nThen wait."]


def test_a_wiki_link_goes_to_the_pages_topic_and_an_external_link_stays():
    (text,) = convert('<p><a rel="mw:WikiLink" href="./Fridges" title="Fridges">parent</a> and '
                      '<a rel="mw:ExtLink" href="https://example.org/x">the manual</a>.</p>')
    assert text == "[parent](/t/external_id/labwiki-Fridges) and [the manual](https://example.org/x)."


def test_nested_lists_keep_their_numbers_and_indents():
    (text,) = convert("<ol><li>Rough pump<ul><li>open V21</li><li>open V16</li></ul></li>"
                      "<li>Wait for P1</li></ol>")
    assert text == "1. Rough pump\n   - open V21\n   - open V16\n2. Wait for P1"


def test_a_small_table_stays_a_table_with_its_first_row_as_header():
    (text,) = convert("<table><tbody><tr><th>MC Power (uW)</th><th>MC Temp (mK)</th></tr>"
                      "<tr><td>200</td><td>81</td></tr><tr><td>250</td><td>9|5</td></tr></tbody></table>")
    assert text == "| MC Power (uW) | MC Temp (mK) |\n|---|---|\n| 200 | 81 |\n| 250 | 9\\|5 |"


def test_a_table_with_long_rows_is_written_out_row_by_row():
    steps = "".join(f"<li>step {i} " + "x" * 60 + "</li>" for i in range(12))
    blocks = convert(f"<table><tbody><tr><td>Step</td><td>Dolan</td><td>Manhattan</td></tr>"
                     f"<tr><td>Baking</td><td><ol>{steps}</ol></td><td>bake at 180C</td></tr></tbody></table>")
    assert blocks[0] == "#### Baking"
    assert blocks[1].startswith("**Dolan**\n\n1. step 0 ") and "\n12. step 11 " in blocks[1]
    assert blocks[2] == "**Manhattan**\n\nbake at 180C"


def test_a_figure_is_its_image_and_caption():
    (text,) = convert('<figure typeof="mw:File/Thumb"><a href="./File:Fridge.png"><img src="./images/thumb/a/ab/'
                      'Fridge.png/300px-Fridge.png" alt="fridge" width="300" height="200"/></a>'
                      "<figcaption>The <b>MXC</b> plate</figcaption></figure>")
    assert text == "![fridge|300x200](upload://300px-Fridge.png)\n*The **MXC** plate*"


def test_maths_becomes_tex_the_forum_renders():
    (text,) = convert('<p>The rate <span class="mwe-math-element"><math alttext="{\\displaystyle \\Gamma=1/T_1}">'
                      "<semantics></semantics></math></span> holds.</p>")
    assert text == "The rate $\\Gamma=1/T_1$ holds."


def test_code_blocks_are_fenced():
    (text,) = convert("<pre>ssh xilinx@192.168.14.184\nls</pre>")
    assert text == "```\nssh xilinx@192.168.14.184\nls\n```"


# ---------------------------------------------------------------- parts


def test_a_page_is_cut_at_its_sections_and_a_heading_goes_with_what_it_heads():
    blocks = [wm.Block("intro " * 50), wm.Block("## A", level=2), wm.Block("a " * 700),
              wm.Block("## B", level=2), wm.Block("b " * 900)]
    cut = wm.parts(blocks, limit=2000)
    assert all(len(p) <= 2000 for p in cut)
    # A short opening shares the first part; a later section starts its own.
    assert "## A" in cut[0] and cut[1].startswith("## B")

    # A section too long for one part goes on in the next, its heading never left behind.
    cut = wm.parts([wm.Block("intro " * 250), wm.Block("## C", level=2), wm.Block("\n".join(["c " * 40] * 60))],
                   limit=2000)
    assert all(len(p) <= 2000 for p in cut) and len(cut) >= 3
    assert not any(p.rstrip().endswith("## C") for p in cut)
    assert sum(p.startswith("## C") for p in cut) == 1


def test_a_long_list_is_cut_before_one_of_its_items_never_inside_one():
    items = "\n".join(f"{i}. step {'x' * 40}\n   - detail {'y' * 40}\n   - more {'z' * 40}" for i in range(1, 60))
    cut = wm.parts([wm.Block(items)], limit=2000)
    assert len(cut) > 1 and all(len(p) <= 2000 for p in cut)
    # Indented, a part would open as a code block.
    assert all(not p[:1].isspace() for p in cut)


def test_a_table_split_across_parts_repeats_its_header():
    head = "| a | b |\n|---|---|"
    rows = "\n".join(f"| {i} | {'y' * 50} |" for i in range(80))
    cut = wm.parts([wm.Block(f"{head}\n{rows}", table_head=head)], limit=2000)
    assert len(cut) > 1 and all(p.startswith(head) for p in cut)


# ---------------------------------------------------------------- mirroring


class FakeWiki:
    base = "https://wiki.example"

    def __init__(self):
        self.html_by_title: dict[str, str] = {}
        self.listed: dict[int, dict] = {}
        self.files: dict[str, tuple[bytes, str]] = {}

    def pages(self, namespaces):
        return dict(self.listed)

    def redirects(self, namespaces):
        return {"BF1": "BlueFors1"}

    def html(self, title):
        return self.html_by_title[title]

    def fetch(self, url):
        return self.files[url]

    def page_url(self, title, fragment=""):
        return f"{self.base}/index.php/{title.replace(' ', '_')}" + (f"#{fragment}" if fragment else "")


class FakeForum:
    username = "wiki-bot"
    dry_run = False

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.topics: dict[str, dict] = {}
        self.uploaded: list[str] = []
        self.next = 100

    def request(self, method, path, data=None, body=None, content_type=None):
        self.calls.append((method, path, data or {}))
        if method == "POST" and path == "/posts.json":
            self.next += 1
            return {"id": self.next, "topic_id": int((data or {}).get("topic_id") or 900 + self.next)}
        return {}

    def topic_by_external_id(self, external_id):
        return self.topics.get(external_id)

    def upload(self, filename, content, mime):
        self.uploaded.append(filename)
        return {"short_url": f"upload://{filename}"}

    def writes(self):
        return [(m, p) for m, p, _ in self.calls]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "POST_INTERVAL", 0)
    wiki, forum = FakeWiki(), FakeForum()
    mirror = wm.Mirror(wiki, forum, tmp_path / "state.json", "13")
    return wiki, forum, mirror


def long_page(n: int) -> str:
    return page("".join(f'<section><h2 id="s{i}">Section {i}</h2><p>{"word " * 500}</p></section>' for i in range(n)))


def test_a_new_page_opens_a_topic_in_parts_bound_to_its_id(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "BlueFors1", "revid": 70}}
    wiki.html_by_title["BlueFors1"] = long_page(3)
    assert mirror.run() == ["BlueFors1"]
    first, *rest = [c for c in forum.calls if c[1] == "/posts.json"]
    assert first[2]["external_id"] == "labwiki-7" and first[2]["category"] == "13"
    assert first[2]["title"] == "BlueFors1 (lab wiki page)"
    assert first[2]["raw"].startswith("*This is the lab wiki's page [BlueFors1](https://wiki.example/index.php/BlueFors1)")
    assert "It is in 3 posts, of which this is the first." in first[2]["raw"]
    assert [c[2]["raw"].split("\n")[0] for c in rest] == [
        "*Part 2 of 3 of the lab wiki's page [BlueFors1](https://wiki.example/index.php/BlueFors1).*",
        "*Part 3 of 3 of the lab wiki's page [BlueFors1](https://wiki.example/index.php/BlueFors1).*"]
    assert all(len(c[2]["raw"]) <= wm.MAX_POST_LENGTH for c in [first, *rest])

    # Nothing changed on the wiki, nothing is written.
    forum.calls.clear()
    assert mirror.run() == [] and forum.calls == []


def test_an_edited_page_edits_only_the_parts_that_changed(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "Long page title here", "revid": 70}}
    wiki.html_by_title["Long page title here"] = long_page(3)
    mirror.run()
    ids = mirror.state["pages"]["7"]["posts"]
    forum.calls.clear()
    wiki.listed[7]["revid"] = 71
    wiki.html_by_title["Long page title here"] = long_page(3).replace("Section 2", "Section two")
    mirror.run()
    assert forum.writes() == [("PUT", f"/posts/{ids[2]}.json")]
    assert forum.calls[0][2]["post[edit_reason]"] == "updated from the wiki"


def test_a_page_that_grows_goes_on_after_the_replies_and_one_that_shrinks_says_so(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "Long page title here", "revid": 70}}
    wiki.html_by_title["Long page title here"] = long_page(2)
    mirror.run()
    forum.calls.clear()
    wiki.listed[7]["revid"] = 71
    wiki.html_by_title["Long page title here"] = long_page(3)
    mirror.run()
    # Every part's opening counts the parts, so each is edited, and the new one is added.
    assert [w for w in forum.writes() if w[0] == "POST"] == [("POST", "/posts.json")]
    assert len(mirror.state["pages"]["7"]["posts"]) == 3

    forum.calls.clear()
    wiki.listed[7]["revid"] = 72
    wiki.html_by_title["Long page title here"] = page("<p>Short now.</p>")
    mirror.run()
    gone = forum.calls[-1][2]["post[raw]"]
    assert "is gone: the page is shorter now" in gone


def test_a_renamed_page_keeps_its_topic_and_a_deleted_one_is_closed(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "Oxford fridge notes", "revid": 70}}
    wiki.html_by_title["Oxford fridge notes"] = page("<p>Cold.</p>")
    mirror.run()
    topic_id = mirror.state["pages"]["7"]["topic_id"]
    wiki.listed = {7: {"title": "Oxford fridge handbook", "revid": 71}}
    wiki.html_by_title["Oxford fridge handbook"] = page("<p>Cold.</p>")
    forum.calls.clear()
    mirror.run()
    assert ("PUT", f"/t/-/{topic_id}.json") in forum.writes()
    assert not any(p == "/posts.json" for _, p in forum.writes())

    wiki.listed = {}
    forum.calls.clear()
    assert mirror.run() == ["Oxford fridge handbook"]
    assert ("PUT", f"/t/{topic_id}/status.json") in forum.writes()
    assert forum.calls[-1][2] == {"status": "closed", "enabled": "true"}
    assert "has been deleted" in forum.calls[0][2]["post[raw]"]

    # Restored, the page reopens its topic and writes its first post again.
    wiki.listed = {7: {"title": "Oxford fridge handbook", "revid": 72}}
    forum.calls.clear()
    mirror.run()
    assert forum.calls[0][1:] == (f"/t/{topic_id}/status.json", {"status": "closed", "enabled": "false"})
    assert ("PUT", f"/posts/{mirror.state['pages']['7']['posts'][0]}.json") in forum.writes()


def test_a_lost_state_file_finds_the_topic_by_its_external_id(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "Oxford fridge notes", "revid": 70}}
    wiki.html_by_title["Oxford fridge notes"] = page("<p>Cold.</p>")
    forum.topics["labwiki-7"] = {"id": 55, "title": "Oxford fridge notes", "post_stream": {"posts": [
        {"id": 501, "post_number": 1, "username": "wiki-bot"},
        {"id": 502, "post_number": 2, "username": "pat"},
        {"id": 503, "post_number": 3, "username": "wiki-bot"}]}}
    mirror.run()
    # The mirror's part is the first post alone; post 3 came after a reply.
    assert forum.writes() == [("PUT", "/posts/501.json")]


def test_links_to_pages_and_redirects_go_to_their_topics_and_images_are_uploaded_once(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "BlueFors1", "revid": 70}, 8: {"title": "Fridges", "revid": 80}}
    img = '<figure><img src="/images/a/ab/Plate.png" alt="plate" width="10" height="20"/></figure>'
    wiki.html_by_title["BlueFors1"] = page(f'<p><a rel="mw:WikiLink" href="./Fridges">up</a></p>{img}')
    wiki.html_by_title["Fridges"] = page(f'<p><a rel="mw:WikiLink" href="./BF1">BF1</a> and '
                                         f'<a rel="mw:WikiLink" href="./Nowhere">nowhere</a></p>{img}')
    wiki.files["https://wiki.example/images/a/ab/Plate.png"] = (b"\x89PNG...", "image/png")
    mirror.run()
    raws = {c[2]["title"]: c[2]["raw"] for c in forum.calls if c[1] == "/posts.json"}
    assert "[up](/t/external_id/labwiki-8)" in raws["BlueFors1 (lab wiki page)"]
    assert "[BF1](/t/external_id/labwiki-7)" in raws["Fridges (lab wiki page)"]
    assert "[nowhere](https://wiki.example/index.php/Nowhere)" in raws["Fridges (lab wiki page)"]
    assert "![plate|10x20](upload://Plate.png)" in raws["Fridges (lab wiki page)"]
    assert forum.uploaded == ["Plate.png"]


def test_a_file_too_big_or_not_an_image_is_linked_on_the_wiki(world):
    wiki, forum, mirror = world
    wiki.files["https://wiki.example/images/b/big.png"] = (b"x" * (wm.MAX_UPLOAD_BYTES + 1), "image/png")
    assert mirror.image("/images/b/big.png", "big", 0, 0) == "[big](https://wiki.example/images/b/big.png)"
    wiki.files["https://wiki.example/images/c/sheet.xlsx"] = (b"PK", "application/vnd.ms-excel")
    assert mirror.image("/images/c/sheet.xlsx", "", 0, 0) == "[image](https://wiki.example/images/c/sheet.xlsx)"
    assert forum.uploaded == []


def test_one_page_that_fails_does_not_stop_the_rest(world):
    wiki, forum, mirror = world
    wiki.listed = {7: {"title": "Broken page here", "revid": 70}, 8: {"title": "Fine page here", "revid": 80}}
    wiki.html_by_title["Fine page here"] = page("<p>Fine.</p>")
    assert mirror.run() == ["Fine page here"]


# ---------------------------------------------------------- the wiki's notices

SECRET = "a-secret-of-some-length"


def signed(body: dict, secret: str = SECRET) -> tuple[bytes, str]:
    import hashlib
    import hmac
    import json
    raw = json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_a_notice_is_taken_only_signed_with_the_secret_and_recent():
    raw, signature = signed({"event": "edit", "at": 1000})
    assert wm.verify_notice(raw, signature, SECRET, now=1010) == ""
    assert "signature" in wm.verify_notice(raw, signed({"event": "edit", "at": 1000}, "another secret")[1], SECRET, now=1010)
    assert "signature" in wm.verify_notice(raw + b" ", signature, SECRET, now=1010)
    assert "signature" in wm.verify_notice(raw, "", SECRET, now=1010)
    assert "from now" in wm.verify_notice(raw, signature, SECRET, now=1000 + wm.HOOK_MAX_AGE + 1)
    assert "from now" in wm.verify_notice(raw, signature, SECRET, now=1000 - wm.HOOK_MAX_AGE - 1)
    untimed, untimed_signature = signed({"event": "edit"})
    assert "no time" in wm.verify_notice(untimed, untimed_signature, SECRET, now=1010)


def test_the_listener_starts_a_run_only_for_a_good_notice():
    import threading
    import urllib.error
    import urllib.request
    noticed = []
    server = wm.ThreadingHTTPServer(("127.0.0.1", 0), wm.hook_handler(SECRET, lambda: noticed.append(1), clock=lambda: 1010))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    def post(raw: bytes, signature: str) -> int:
        request = urllib.request.Request(url, data=raw, headers={"X-Qupa-Signature": signature})
        try:
            return urllib.request.urlopen(request, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        raw, signature = signed({"event": "edit", "at": 1000})
        assert post(raw, "sha256=0") == 403 and noticed == []
        assert post(b"x" * (wm.HOOK_MAX_BODY + 1), signature) == 413 and noticed == []
        assert post(raw, signature) == 202 and noticed == [1]
        assert urllib.request.urlopen(url + "health", timeout=5).read() == b"ok"
    finally:
        server.shutdown()


def test_notices_during_a_run_start_one_more_run_after_it():
    import threading
    import time
    started, release = [], threading.Event()

    def run():
        started.append(time.monotonic())
        if len(started) == 1:
            release.wait(5)

    runs = wm.Runs(run, settle=0)
    threading.Thread(target=runs.loop, daemon=True).start()
    runs.notify()
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)
    for _ in range(3):
        runs.notify()
    release.set()
    deadline = time.monotonic() + 5
    while len(started) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    assert len(started) == 2
