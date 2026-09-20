/** Local-only Chrome DevTools acceptance test. Start fixture server on 4317 and Chrome CDP on 9317. */
import assert from "node:assert/strict";
const endpoint = process.env.DELIVERY_CDP_URL ?? "http://127.0.0.1:9317";
const target = (await (await fetch(`${endpoint}/json/list`)).json()).find((entry) => entry.type === "page");
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve) => socket.addEventListener("open", resolve, { once: true }));
let sequence = 0;
const pending = new Map();
socket.addEventListener("message", ({ data }) => {
  const message = JSON.parse(data);
  const pair = pending.get(message.id);
  if (pair) {
    pending.delete(message.id);
    if (message.error) pair.reject(message.error);
    else pair.resolve(message.result);
  }
});
const cdp = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
const evaluate = async (expression) => {
  const response = await cdp("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
  return response.result.value;
};
const until = async (expression) =>
  evaluate(
    `new Promise((resolve,reject)=>{const start=Date.now();const tick=()=>{let ready=false;try{ready=Boolean(${expression})}catch{}if(ready)resolve(true);else if(Date.now()-start>10000)reject(new Error('Timed out: '+${JSON.stringify(expression)}));else setTimeout(tick,40)};tick()})`
  );
const click = async (text) => {
  await until(`[...document.querySelectorAll('button')].some(e=>e.textContent.includes(${JSON.stringify(text)}))`);
  await evaluate(
    `[...document.querySelectorAll('button')].find(e=>e.textContent.includes(${JSON.stringify(text)})).click()`
  );
};
const change = async (selector, value) =>
  evaluate(
    `(()=>{const element=document.querySelector(${JSON.stringify(selector)});const proto=element.tagName==='SELECT'?HTMLSelectElement.prototype:element.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(element,${JSON.stringify(value)});element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));})()`
  );
try {
  await cdp("Page.navigate", { url: "http://127.0.0.1:4317/" });
  await until(`document.body.innerText.includes('2 / 2 items')`);
  assert.equal(await evaluate(`document.body.innerText.includes('Unverified')`), true);
  await click("Completed work");
  await until(`document.querySelector('[role=dialog]')`);
  assert.match(await evaluate(`document.querySelector('[role=dialog]').innerText`), /Code change/);
  await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
  await until(`!document.querySelector('[role=dialog]')`);
  await change('select[aria-label="Approved scope"]', "scope-2");
  await until(`document.body.innerText.includes('No applicable work')`);
  assert.equal(await evaluate(`document.body.innerText.includes('100%')`), false);
  await change('select[aria-label="Approved scope"]', "scope-1");
  await until(`document.body.innerText.includes('2 / 2 items')`);
  await click("Exact release");
  await click("decision-1");
  await until(`document.querySelector('[role=dialog]').innerText.includes('payload-123')`);
  await change('[role="dialog"] select', "allow");
  await change(
    '[role="dialog"] textarea',
    "Reviewed exact artifact and plan.\nExecution uses the recorded conditions."
  );
  assert.equal(await evaluate(`window.deliveryFixture.commands.length`), 0, "typing/newlines must not submit");
  await until(`document.querySelector('[role=dialog] input[type=datetime-local]')`);
  const expiry = new Date(Date.now() + 3600_000);
  const localExpiry = new Date(expiry.getTime() - expiry.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
  await evaluate(`document.querySelector('[role=dialog] input[type=checkbox]').click()`);
  await evaluate(
    `(()=>{const inputs=[...document.querySelectorAll('[role=dialog] input[type=datetime-local]')];const input=inputs.at(-1);Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(localExpiry)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`
  );
  await until(`!document.querySelector('[role=dialog] button[type=submit]').disabled`);
  await click("Record decision");
  await until(`window.deliveryFixture.commands.length===1`);
  const command = await evaluate(`window.deliveryFixture.commands[0]`);
  assert.equal(command.subject_type, "decision");
  assert.equal(command.payload.payload_digest, "payload-123");
  assert.equal(command.payload.decision, "allow");
  assert.equal(command.payload.answer_kind, "reason");
  assert.equal(command.expected_version, 1);
  assert.ok(command.payload.expires_at);
  await click("Close");
  await evaluate(`window.deliveryFixture.setStale()`);
  await click("Refresh");
  await until(`document.body.innerText.includes('Stale or unavailable evidence')`);
  await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  assert.equal(
    await evaluate(`document.documentElement.scrollWidth <= 390`),
    true,
    "mobile layout must not overflow horizontally"
  );
  await cdp("Page.navigate", { url: "http://127.0.0.1:4317/" });
  await until(`document.body.innerText.includes('2 / 2 items')`);
  await click("Exact release");
  await click("decision-1");
  await until(`document.querySelector('[role=dialog] textarea')`);
  await change('[role="dialog"] textarea', "Please clarify the changed operation.");
  await evaluate(`document.querySelector('[role=dialog] input[type=checkbox]').click()`);
  await evaluate(`window.deliveryFixture.rejectVersion()`);
  await click("Record decision");
  await until(`document.querySelector('[role=dialog]').innerText.includes('stale_version')`);
  assert.equal(await evaluate(`window.deliveryFixture.commands.length`), 1);
  assert.equal(await evaluate(`document.querySelector('[role=dialog]').innerText.includes('Decision changed')`), true);
  await click("Close");
  await evaluate(`window.deliveryFixture.showWorkQuestions()`);
  await click("Refresh");
  await until(`document.querySelector('[aria-label="Work question first"] textarea')`);
  await change(
    '[aria-label="Work question first"] textarea',
    "Use the reviewed approach.\nContinue review after all prerequisites are resolved."
  );
  await change('[aria-label="Work question first"] select', "actionable");
  await click("Send answer");
  await until(`!document.querySelector('[aria-label="Work question first"]')`);
  assert.equal(
    await evaluate(`window.deliveryFixture.waitingWork.state`),
    "Awaiting Human",
    "one answer must not clear a second blocker"
  );
  assert.equal(await evaluate(`window.deliveryFixture.waitingWork.next_action`), "review");
  await evaluate(`document.querySelector('[aria-label="Work question second"] details').open=true`);
  await change(
    '[aria-label="Work question second"] details textarea',
    "Prerequisite is no longer needed under the approved scope."
  );
  await change('[aria-label="Work question second"] details input', "verified-obsolete-receipt");
  await click("Withdraw question with evidence");
  await until(`window.deliveryFixture.waitingWork.state==='Todo'`);
  assert.equal(
    await evaluate(`window.deliveryFixture.waitingWork.next_action`),
    "review",
    "withdrawal must retain reviewer routing"
  );
  console.log(
    "PASS: counts, unknowns, keyboard, filters, exact decisions, newline, stale/conflict handling, mobile, multiple questions, requester withdrawal, reviewer continuation"
  );
} finally {
  socket.close();
}
