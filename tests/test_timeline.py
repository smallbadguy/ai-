"""Execute the real timeline controller in Node with a small DOM stand-in."""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const N = input.n, PRESENT = !!input.present, PU = {}, S = {i:0, speed:1, tz:'bj'};
const E = Array.from({length:N}, (_, i) => ({title:'Event ' + i, ts:'2026-01-01T00:00:00Z',
  act:i < N/2 ? 'a1' : 'a2', baton:i % 13 === 0, compact:i % 2 === 0}));
const D = {acts:[{id:'a1'}, {id:'a2'}]};
const esc = s => s, svgPlay = '', svgPause = '';
const fmt = () => ({m:1, d:1, hm:'00:00', s:'00'}), human = () => '';
let width = input.width, barNodes = [], timers = new Map(), timerId = 0, observer;
const listeners = {}, callbacks = {};
const head = {style:{}}, ordinary = {style:{}, setAttribute(){}};
const bars = {
  style:{}, html:'', getBoundingClientRect:() => ({left:100, width}),
  set innerHTML(html) {
    this.html = html;
    barNodes = [...html.matchAll(/<i class="b" data-start="(\d+)" data-end="(\d+)" style="--h:(\d+)%;left:calc\([^\"]*\);width:([\d.e+-]+)px"/g)].map(m => ({
      dataset:{start:m[1], end:m[2]}, height:+m[3], width:+m[4],
      classes:new Set(), classList:{toggle(c, on){on ? this.owner.classes.add(c) : this.owner.classes.delete(c);}},
    }));
    for (const b of barNodes) b.classList.owner = b;
  }
};
const strip = {innerHTML:'', addEventListener:(k,fn) => {listeners[k]=fn;}, setPointerCapture(){}};
const $ = k => k === '#strip .bars' ? bars : k === '#strip' ? strip : k === '#head' ? head : ordinary;
const $$ = k => k === '#strip .b' ? barNodes : [];
global.window = global;
global.addEventListener = (k,fn) => {callbacks[k]=fn;};
global.ResizeObserver = class {constructor(fn){observer=fn;} observe(){}};
global.setTimeout = fn => {timers.set(++timerId,fn); return timerId;};
global.clearTimeout = id => timers.delete(id);
function pause() {}
function go(i) {S.i=Math.max(0,Math.min(N-1,i)); updCtl();}
eval(input.code);
buildCtl(); updCtl();
function snapshot() {
  return {n:N, width, right:bars.style.right, current:S.i, head:head.style.left,
    bars:barNodes.map(b=>({start:+b.dataset.start, end:+b.dataset.end, width:b.width,
      height:b.height, on:b.classes.has('on'), cur:b.classes.has('cur')}))};
}
const initial = snapshot(), picks = [];
for (const fraction of [0, .25, .5, .75, 1]) {
  listeners.pointerdown({clientX:100+width*fraction,pointerId:1});
  listeners.pointerup(); picks.push(snapshot());
}
const resized = [];
for (const w of input.resizes || []) {
  width=w; callbacks.resize(); observer();
  const pending=[...timers.values()]; timers.clear(); pending.forEach(fn=>fn());
  resized.push(snapshot());
}
console.log(JSON.stringify({initial,picks,resized}));
"""


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the UI controller")
class Timeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template = (ROOT / "src/template.html").read_text(encoding="utf-8")
        cls.code = template.split("function buildCtl() {", 1)[1].split("/* 左侧成员 */", 1)[0]
        cls.code = "function buildCtl() {" + cls.code

    def run_ui(self, n, width, present=False, resizes=()):
        result = subprocess.run(["node", "-e", HARNESS], input=json.dumps(dict(
            code=self.code, n=n, width=width, present=present, resizes=resizes)),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def check_geometry(self, frame):
        n, width, bars = frame["n"], frame["width"], frame["bars"]
        self.assertTrue(bars)
        self.assertLessEqual(len(bars), n)
        next_step = 0
        for bar in bars:
            start, end = bar["start"], bar["end"]
            self.assertEqual(start, next_step, "Grouping must not skip or duplicate any step")
            self.assertGreaterEqual(end, start)
            next_step = end + 1
            center = (start + end + 1) / 2 / n * width
            self.assertGreaterEqual(center - bar["width"] / 2, -0.001)
            self.assertLessEqual(center + bar["width"] / 2, width + 0.001)
            peak = max(100 if i % 13 == 0 else 34 if i % 2 == 0 else 66
                       for i in range(start, end + 1))
            self.assertEqual(bar["height"], peak, "Handoffs must remain visible inside a group")
        self.assertEqual(next_step, n)
        active = [b for b in bars if b["cur"]]
        self.assertEqual(len(active), 1)
        self.assertLessEqual(active[0]["start"], frame["current"])
        self.assertGreaterEqual(active[0]["end"], frame["current"])
        self.assertTrue(all(b["on"] == (b["start"] <= frame["current"]) for b in bars))
        self.assertEqual(frame["head"], f"calc(({frame['current']} + .5) / {n} * 100%)")

    def test_dense_timeline_and_clicks_keep_all_steps_reachable(self):
        for width in (276.04, 751.3, 1265.7):
            with self.subTest(width=width):
                result = self.run_ui(1373, width)
                self.check_geometry(result["initial"])
                self.assertLess(len(result["initial"]["bars"]), 1373)
                self.assertEqual([p["current"] for p in result["picks"]], [0, 343, 686, 1029, 1372])
                for frame in result["picks"]:
                    self.check_geometry(frame)

    def test_resize_rebuckets_and_preserves_the_playback_position(self):
        result = self.run_ui(1373, 751, resizes=(1265, 276, 0, 751))
        self.assertEqual([len(r["bars"]) for r in result["resized"]], [632, 138, 1, 375])
        for frame in result["resized"]:
            self.assertEqual(frame["current"], 1372)
            self.check_geometry(frame)

    def test_short_business_replay_keeps_one_bar_per_step(self):
        result = self.run_ui(209, 800, present=True)
        self.assertEqual(len(result["initial"]["bars"]), 209)
        self.assertEqual(result["initial"]["right"], "8px")
        self.check_geometry(result["initial"])

    def test_single_step_and_narrow_or_hidden_strips(self):
        for n, width in ((1, 700), (1, 0), (432, 751), (1373, 0.5)):
            with self.subTest(n=n, width=width):
                self.check_geometry(self.run_ui(n, width)["initial"])
