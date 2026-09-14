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
    def test_copy_prompt_clipboard_and_fallbacks(self):
        result = subprocess.run(["node", "-"], cwd=Path(__file__).resolve().parents[1],
            input=r'''const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync("candytest/static/app.js", "utf8");
const template = fs.readFileSync("candytest/templates/index.html", "utf8");
assert.match(template, /id="copyPrompt" type="button"/);
assert.ok(source.includes('$("#copyPrompt").onclick = copyPrompt;'));
const button = { focus() {} };
const form = { question_id: { value: "thibault_sottiaux" }, random_candy_format: { value: "original" } };
let copied, message, manual, removed = 0, fallback = true, failure = false;
const context = vm.createContext({
  $: s => s === "#copyPrompt" ? button : form, URLSearchParams,
  api: async url => { assert.ok(url.includes("question_id=" + form.question_id.value));
    if (failure) throw new Error("API failed"); return { prompt: "Full prompt" }; },
  navigator: { clipboard: { writeText: async text => { copied = text; } } },
  document: { body: { append() {} }, createElement: () => ({ setAttribute() {}, focus() {},
    select() {}, remove() { removed++; } }), execCommand: () => fallback },
  window: { prompt: (label, text) => { manual = text; } },
  flash: text => { message = text; }
});
vm.runInContext(source.slice(source.indexOf("  async function copyPrompt()"), source.indexOf("  function accuracyBadge")), context);
(async () => {
  await context.copyPrompt();
  assert.equal(copied, "Full prompt"); assert.equal(button.disabled, false);
  assert.equal(message, "题目提示词已复制。"); assert.equal(removed, 0);
  context.navigator.clipboard.writeText = async () => { throw new Error("denied"); };
  form.question_id.value = "candy"; form.random_candy_format.value = "true";
  await context.copyPrompt(); assert.equal(removed, 1); assert.match(message, /重新生成/);
  context.navigator.clipboard = undefined; fallback = false;
  await context.copyPrompt(); assert.equal(manual, "Full prompt"); assert.match(message, /未成功/);
  failure = true; await context.copyPrompt(); assert.equal(message, "API failed");
  assert.equal(button.disabled, false); assert.equal(button.textContent, "复制题目提示词");
})().catch(e => { console.error(e); process.exitCode = 1; });
''', text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
for (const question of ['thibault_sottiaux', 'johannes_heidecke', 'sam_mccandlish']) {
elements["#question"].value = question;
context.setQuestionDefaults();
assert.equal(elements["#modelOverride"].value, "gpt-6-astra");
assert.equal(elements["#effort"].value, "low");
assert.equal(elements["#rounds"].value, 5);
}
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
