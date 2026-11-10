/**
 * Run Next on 127.0.0.1:3107, then run this script and open http://127.0.0.1:3108.
 * Only localhost proxy HTML receives mock SDK/API code; production has no test route.
 */
import { createServer } from "node:http";
import { createFixture, installHarness } from "./specification-review-fixture.mjs";
import { runBrowserJourneys } from "./specification-review-browser-journeys.mjs";

const port = Number(process.env.SPECIFICATION_REVIEW_HARNESS_PORT || 3108);
const target = new URL(process.env.SPECIFICATION_REVIEW_HARNESS_TARGET || "http://127.0.0.1:3107");
if (!["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)) throw new Error("The review harness only forwards to localhost.");
const fixtures = { new_enquiry: createFixture(), amendment: createFixture("amendment"), ambiguous: createFixture("ambiguous"), second_item: createFixture("new_enquiry", "102") };
const mockScript = `(${installHarness.toString()})(${JSON.stringify(fixtures)});`;
const journeyScript = `window.runSpecificationReviewJourneys = ${runBrowserJourneys.toString()};`;
const controls = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Specification Review browser harness</title><style>body{margin:0;font:14px system-ui;background:#f7f6f4;color:#252529}header{padding:12px;border-bottom:1px solid #ddd}h1{font-size:16px;margin:0 0 8px}button{font:inherit;background:white;border:1px solid #aaa;border-radius:5px;padding:6px 9px;margin:3px;cursor:pointer}button:focus-visible{outline:3px solid #a02f27;outline-offset:2px}iframe{display:block;width:100%;height:calc(100vh - 168px);border:0}#notice{min-height:20px;margin:6px 3px;font-size:12px}#requests{position:fixed;right:12px;bottom:12px;max-width:min(650px,90vw);max-height:50vh;overflow:auto;background:white;border:1px solid #aaa;padding:15px;z-index:5;white-space:pre-wrap;font-size:11px}#requests[hidden]{display:none}</style></head><body><header><h1>Local test controls — actual Specification Review route below</h1><div><button data-scenario="new_enquiry">New enquiry</button><button data-scenario="amendment">Amendment / detected reference</button><button data-scenario="ambiguous">Ambiguous roofs</button><button data-action="regeneration_failure">Next regeneration fails</button><button data-action="concurrent_save">Next save conflicts</button><button data-action="sources_change">Sources change</button><button data-action="session_expiry">Session expires</button><button data-action="switch_item">Switch item</button><button data-action="leave_landing_zone">Leave Landing Zone</button><button data-action="publish">Publish matching PDF</button><button id="inspect" data-action="inspect">Inspect requests</button></div><p id="notice" role="status">Mocks are local; no Monday/model/storage requests are made.</p></header><iframe id="review" src="/specification-review?scenario=new_enquiry" title="Actual Specification Review workspace"></iframe><pre id="requests" hidden></pre><script>const frame=document.getElementById('review');const notice=document.getElementById('notice');const log=document.getElementById('requests');document.addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;if(button.dataset.scenario){frame.src='/specification-review?scenario='+button.dataset.scenario;log.hidden=true;notice.textContent='Loading '+button.dataset.scenario;return;}if(button.id==='inspect')log.hidden=!log.hidden;frame.contentWindow.postMessage({source:'specification-review-controls',action:button.dataset.action},location.origin);});window.addEventListener('message',event=>{if(event.origin!==location.origin||event.source!==frame.contentWindow||event.data?.source!=='specification-review-harness')return;if(event.data.type==='requests')log.textContent=JSON.stringify(event.data.data,null,2);else if(event.data.type==='notice')notice.textContent=event.data.data;else if(event.data.type==='ready')notice.textContent='Ready: '+event.data.data.scenario;});</script></body></html>`;

const runnerControls = controls
  .replace('id="inspect" data-action="inspect">Inspect requests</button>', 'id="inspect" data-action="inspect">Inspect requests</button><button id="run-journeys">Run browser journeys</button>')
  .replace('<pre id="requests" hidden>', '<pre id="results" role="status" style="white-space:pre-wrap;margin:10px;padding:10px;border:1px solid #aaa" hidden></pre><pre id="requests" hidden>')
  .replace('<script>const frame=', '<script src="/__specification-review-journeys.js"></script><script>let harnessRequests=[];const frame=')
  .replace("if(button.dataset.scenario){", `if(button.id==='run-journeys'){button.disabled=true;const output=document.getElementById('results');output.hidden=false;window.runSpecificationReviewJourneys(frame,action=>frame.contentWindow.postMessage({source:'specification-review-controls',action},location.origin),()=>harnessRequests,result=>{output.textContent=JSON.stringify(result,null,2);if(result.status==='passed'||result.status==='failed')fetch('/__specification-review-browser-results',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(result)});}).finally(()=>button.disabled=false);return;}if(button.dataset.scenario){`)
  .replace("if(event.data.type==='requests')log.textContent=JSON.stringify(event.data.data,null,2);", "if(event.data.type==='requests'){harnessRequests=event.data.data;log.textContent=JSON.stringify(event.data.data,null,2);}");
let browserResult = { status: "not_run" };
const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://127.0.0.1:${port}`);
  if (url.pathname === "/") { response.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" }); response.end(runnerControls); return; }
  if (url.pathname === "/__specification-review-harness.js") { response.writeHead(200, { "Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-store" }); response.end(mockScript); return; }
  if (url.pathname === "/__specification-review-journeys.js") { response.writeHead(200, { "Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-store" }); response.end(journeyScript); return; }
  if (url.pathname === "/__specification-review-browser-results") {
    if (request.method === "POST") {
      const parts = []; let size = 0;
      for await (const part of request) { size += part.length; if (size > 65536) { response.writeHead(413); response.end(); return; } parts.push(part); }
      try { browserResult = JSON.parse(Buffer.concat(parts).toString("utf8")); } catch { response.writeHead(400); response.end(); return; }
    }
    response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" }); response.end(JSON.stringify(browserResult)); return;
  }
  try {
    const upstream = await fetch(new URL(url.pathname + url.search, target), { headers: { Accept: request.headers.accept || "*/*", "Accept-Encoding": "identity" } });
    const headers = Object.fromEntries(upstream.headers);
    delete headers["content-length"]; delete headers["content-encoding"]; delete headers["transfer-encoding"];
    headers["cache-control"] = "no-store";
    let bytes = Buffer.from(await upstream.arrayBuffer());
    if ((headers["content-type"] || "").includes("text/html")) bytes = Buffer.from(bytes.toString("utf8").replace("<head>", '<head><script src="/__specification-review-harness.js"></script>'));
    response.writeHead(upstream.status, headers); response.end(bytes);
  } catch {
    response.writeHead(502, { "Content-Type": "text/plain" }); response.end(`Start Next at ${target.origin} before opening this harness.`);
  }
});
server.listen(port, "127.0.0.1", () => process.stdout.write(`Specification Review harness: http://127.0.0.1:${port}\nForwarding real route from ${target.origin}\n`));
