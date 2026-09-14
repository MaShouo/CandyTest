"""Offline checks for frontend selection and start-button feedback (requires Node)."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


class FrontendInteractionTests(unittest.TestCase):
    def test_fixed_navigation_targets_exist(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "candytest/templates/index.html").read_text(encoding="utf-8")
        style = (root / "candytest/static/style.css").read_text(encoding="utf-8")
        nav = re.search(r'<nav class="workspace-nav".*?</nav>', template, re.S).group()
        targets = re.findall(r'href="#([^"]+)"', nav)
        self.assertEqual(targets, ["gateways", "testSetup", "currentTask", "history"])
        for target in targets:
            self.assertEqual(template.count(f'id="{target}"'), 1)
        nav_style = re.search(r"\.workspace-nav\s*\{([^}]+)\}", style).group(1)
        self.assertRegex(nav_style, r"position:\s*fixed")
        self.assertRegex(nav_style, r"z-index:\s*40")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend checks")
    def test_selection_estimate_and_job_controls(self):
        result = subprocess.run(
            ["node", "-"], cwd=Path(__file__).resolve().parents[1],
            input=r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("candytest/static/app.js", "utf8");
const elements = {
  "#engine": { value: "pi" }, "#rounds": { value: "5" },
  "#startJob": {}, "#testEstimate": {}, "#stopJob": {},
  "#selectAllGateways": {}, "#selectionSummary": {},
};
const checkboxes = [{ checked: false }, { checked: false }];
const state = { runtime: { engines: { pi: true, codex: false } },
  gateways: [{}, {}, {}], currentJob: null, starting: false };
const context = vm.createContext({ state, $: selector => elements[selector],
  document: { querySelectorAll: selector => selector.includes(":checked")
    ? checkboxes.filter(box => box.checked) : checkboxes } });
function load(name, next) {
  vm.runInContext(source.slice(source.indexOf(`  function ${name}(`),
    source.lastIndexOf("\n", source.indexOf(`function ${next}(`))), context);
}
load("updateTestEstimate", "updateJobControls");
load("updateJobControls", "openDetailKeys");
load("syncGatewaySelectAll", "saveGatewayOrder");
const sync = () => context.syncGatewaySelectAll();
sync();
assert.equal(elements["#startJob"].disabled, true);
assert.match(elements["#testEstimate"].textContent, /勾选/);
assert.match(elements["#selectionSummary"].textContent, /0 \/ 2.*1 个已停用/);
checkboxes[0].checked = true;
sync();
assert.equal(elements["#selectAllGateways"].indeterminate, true);
assert.equal(elements["#startJob"].disabled, false);
assert.match(elements["#testEstimate"].textContent, /共 5 次/);
checkboxes[1].checked = true;
elements["#rounds"].value = "3";
sync();
assert.equal(elements["#selectAllGateways"].checked, true);
assert.equal(elements["#selectAllGateways"].indeterminate, false);
assert.match(elements["#testEstimate"].textContent, /共 6 次/);
for (const rounds of ["", "0", "101", "1.5"]) {
  elements["#rounds"].value = rounds;
  sync();
  assert.match(elements["#testEstimate"].textContent, /1–100/);
}
elements["#rounds"].value = "5";
for (const status of ["queued", "running", "cancelling", "completed", "failed", "cancelled"]) {
  state.currentJob = {status};
  context.updateJobControls(state.currentJob);
  const active = ["queued", "running", "cancelling"].includes(status);
  assert.equal(elements["#startJob"].disabled, active);
  assert.equal(elements["#stopJob"].hidden, !active);
  assert.equal(elements["#stopJob"].disabled, status === "cancelling");
}
state.currentJob = null;
state.starting = true;
sync();
assert.equal(elements["#startJob"].disabled, true);
assert.equal(elements["#startJob"].textContent, "正在启动…");
state.starting = false;
elements["#engine"].value = "codex";
sync();
assert.equal(elements["#startJob"].disabled, true);
assert.match(elements["#testEstimate"].textContent, /引擎不可用/);
vm.runInContext(source.match(/  const questionDefaults = .*;/)[0], context);
elements["#question"] = { value: "thibault_sottiaux" };
elements["#modelOverride"] = { value: "old-model" };
elements["#randomCandyFormat"] = { style: {} };
context.setEfforts = value => { elements["#effort"] = { value }; };
load("setQuestionDefaults", "renderEngines");
context.setQuestionDefaults();
assert.equal(elements["#modelOverride"].value, "gpt-6-astra");
assert.equal(elements["#effort"].value, "low");
assert.equal(elements["#rounds"].value, 3);
elements["#question"].value = "candy";
context.setQuestionDefaults();
assert.equal(elements["#modelOverride"].value, "");
assert.equal(elements["#rounds"].value, 5);
console.log("Frontend interaction checks passed");
''', text=True, encoding="utf-8", capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
