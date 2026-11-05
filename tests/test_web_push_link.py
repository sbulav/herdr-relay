"""The pane link a blocked push carries, as the browser client follows it (#96).

The relay sends each blocked push with `url: "/?pane=<id>&host_id=<id>"`. A tap reaches the
page in one of two ways (`web/sw.js`): with no window open the service worker opens that URL,
and with one open it posts `{type: "navigate", url}` to it. Either way the page must open that
pane once, after the next agents snapshot.

The page's "Push links" section runs under node with the page globals it touches stubbed out.
Skipped, not failed, when node is missing; `nix develop` provides it.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGE = (REPO / "web" / "index.html").read_text(encoding="utf-8")
WORKER = (REPO / "web" / "sw.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _section():
    start = PAGE.index("// --- Push links ---")
    return PAGE[start:PAGE.index("// --- Web Push ---", start)]


def _pane_key():
    return re.search(r"^function paneKey\(value\) \{.*?^\}", PAGE, re.S | re.M).group(0)


HARNESS = """
// node has its own read-only `navigator`, so the stubs replace the globals outright.
const listeners = [];
const stub = (name, value) => Object.defineProperty(globalThis, name, {value, writable: true, configurable: true});
stub('location', {href: %(start)s});
stub('history', {
  state: null, replaced: [],
  replaceState(state, title, url) { this.replaced.push(url); location.href = new URL(url, location.href).href; },
});
stub('navigator', {serviceWorker: {addEventListener(type, fn) { if (type === 'message') listeners.push(fn); }}});
let agents = [], activePane = null;
const opened = [], closed = [];
function openTerminal(paneId, hostId) { activePane = {pane_id: paneId, host_id: hostId}; opened.push([paneId, hostId]); }
function closeTerminal() { closed.push(activePane.pane_id); activePane = null; }
// What handleMessage does with an agents frame, as far as the link is concerned.
function snapshot(list) { agents = list; openPendingPane(); }
function navigate(url) { listeners.forEach(fn => fn({data: {type: 'navigate', url}})); }
function agent(paneId, hostId) { return {pane_id: paneId, host_id: hostId, host: hostId, status: 'blocked'}; }
%(pane_key)s
%(section)s
%(steps)s
console.log(JSON.stringify({opened, closed, href: location.href, replaced: history.replaced}));
"""


@unittest.skipIf(NODE is None, "node is not installed")
class PushLinkTests(unittest.TestCase):
    def run_page(self, steps, start="https://relay.test/"):
        script = HARNESS % {
            "start": json.dumps(start), "pane_key": _pane_key(), "section": _section(), "steps": steps,
        }
        result = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_pane_link_opens_that_pane_on_the_first_snapshot(self):
        page = self.run_page(
            "snapshot([agent('w1-p1', 'box'), agent('w1-p2', 'box')]);",
            start="https://relay.test/?pane=w1-p2&host_id=box",
        )
        self.assertEqual(page["opened"], [["w1-p2", "box"]])

    def test_the_host_picks_between_panes_with_the_same_id(self):
        page = self.run_page(
            "snapshot([agent('w1-p1', 'box'), agent('w1-p1', 'local')]);",
            start="https://relay.test/?pane=w1-p1",
        )
        self.assertEqual(page["opened"], [["w1-p1", "local"]])

    def test_the_link_leaves_the_address_once_followed(self):
        page = self.run_page(
            "snapshot([agent('w1-p2', 'box')]);",
            start="https://relay.test/?token=t&pane=w1-p2&host_id=box#top",
        )
        self.assertEqual(page["href"], "https://relay.test/?token=t#top")
        self.assertEqual(page["replaced"], ["/?token=t#top"])

    def test_a_link_to_a_pane_the_relay_does_not_list_stays_on_the_list(self):
        page = self.run_page(
            "snapshot([agent('w1-p1', 'box')]);",
            start="https://relay.test/?pane=w9-p9&host_id=box",
        )
        self.assertEqual(page["opened"], [])
        self.assertEqual(page["href"], "https://relay.test/")

    def test_a_pane_that_appears_after_the_first_snapshot_does_not_open(self):
        """The first snapshot is the whole herd, so a link it cannot match is spent."""
        page = self.run_page(
            "snapshot([]); snapshot([agent('w1-p2', 'box')]);",
            start="https://relay.test/?pane=w1-p2&host_id=box",
        )
        self.assertEqual(page["opened"], [])

    def test_the_link_opens_the_pane_once(self):
        """Back to the list, then the next snapshot: the reader stays where they went."""
        page = self.run_page(
            "snapshot([agent('w1-p2', 'box')]); closeTerminal(); snapshot([agent('w1-p2', 'box')]);",
            start="https://relay.test/?pane=w1-p2&host_id=box",
        )
        self.assertEqual(page["opened"], [["w1-p2", "box"]])

    def test_a_navigate_message_before_the_first_snapshot_waits_for_it(self):
        page = self.run_page(
            "navigate('/?pane=w1-p2&host_id=box');"
            "const before = opened.length;"
            "snapshot([agent('w1-p2', 'box')]);"
            "if (before !== 0) throw new Error('opened before the snapshot');"
        )
        self.assertEqual(page["opened"], [["w1-p2", "box"]])

    def test_a_navigate_message_after_a_snapshot_opens_at_once(self):
        page = self.run_page("snapshot([agent('w1-p2', 'box')]); navigate('/?pane=w1-p2&host_id=box');")
        self.assertEqual(page["opened"], [["w1-p2", "box"]])

    def test_a_navigate_message_switches_from_another_open_session(self):
        page = self.run_page(
            "snapshot([agent('w1-p1', 'box'), agent('w1-p2', 'box')]);"
            "openTerminal('w1-p1', 'box');"
            "navigate('https://relay.test/?pane=w1-p2&host_id=box');"
        )
        self.assertEqual(page["closed"], ["w1-p1"])
        self.assertEqual(page["opened"], [["w1-p1", "box"], ["w1-p2", "box"]])

    def test_a_navigate_message_with_no_pane_leaves_the_session_open(self):
        page = self.run_page(
            "snapshot([agent('w1-p1', 'box')]); openTerminal('w1-p1', 'box'); navigate('/');"
        )
        self.assertEqual(page["closed"], [])

    def test_malformed_links_are_ignored(self):
        page = self.run_page(
            "snapshot([agent('w1-p1', 'box')]);"
            "navigate('http://[');"
            "navigate(42);"
            "listeners.forEach(fn => fn({data: null}));"
            "navigate('/?pane=' + encodeURIComponent(\"x');alert(1)//\"));"
        )
        self.assertEqual(page["opened"], [])

    def test_the_link_never_reaches_markup(self):
        self.assertNotRegex(_section(), r"innerHTML|outerHTML|insertAdjacentHTML|document\.write")

    def test_the_page_follows_the_link_after_each_agents_snapshot(self):
        branch = re.search(r"if \(msg\.type === 'agents'\) \{.*?\n  \} else if", PAGE, re.S).group(0)
        self.assertLess(branch.index("agents = msg.agents"), branch.index("openPendingPane();"))

    def test_the_worker_and_the_relay_speak_the_same_link(self):
        self.assertIn("client.postMessage({ type: 'navigate', url });", WORKER)
        self.assertIn("self.clients.openWindow(url)", WORKER)
        transport = (REPO / "relay" / "herdr_relay" / "transport.py").read_text(encoding="utf-8")
        self.assertIn('url=f"/?pane={pid}&host_id={host_id}"', transport)


if __name__ == "__main__":
    unittest.main()
