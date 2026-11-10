/** Runs against real rendered React controls through the local proxy harness. */
export async function runBrowserJourneys(frame, send, latestRequests, showResult) {
  const results = [];
  let scenario = "initializing";
  let stage = "starting browser journeys";
  const labels = ["Scale", "Page Size", "Drawing Reference", "Drawing Title", "Bauder Contract Number", "Bauder Roof Num", "Incoming Information", "Revision", "Date Received", "Company", "Contact", "Surveyor", "Target U Value", "Target Min U Value", "Membrane", "Tapered Insulation", "VCL", "Decking"];
  const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const documentOf = () => frame.contentDocument;
  const text = () => documentOf()?.body?.textContent || "";
  const assert = (condition, message) => { if (!condition) throw new Error(message); results.push(message); showResult({ status: "running", assertions: results.length, last: message }); };
  const waitFor = async (condition, message, timeout = 15000) => {
    stage = `waiting for ${message}`;
    const end = Date.now() + timeout;
    while (Date.now() < end) { if (condition()) return; await delay(50); }
    throw new Error(`Timed out: ${message}`);
  };
  const buttons = (root = documentOf()) => Array.from(root?.querySelectorAll("button") || []);
  const button = (label, root) => buttons(root).find((element) => element.textContent.trim() === label);
  const click = async (label, root) => { stage = `clicking ${label}`; const element = button(label, root); if (!element || element.disabled) throw new Error(`Unavailable button: ${label}`); element.click(); await delay(30); };
  const fieldset = (label) => Array.from(documentOf()?.querySelectorAll("fieldset") || []).find((element) => element.querySelector("legend")?.textContent.trim() === label);
  function setInput(element, value) {
    if (!element) throw new Error("Missing editable input");
    const win = frame.contentWindow;
    const prototype = element.tagName === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : element.tagName === "SELECT" ? win.HTMLSelectElement.prototype : win.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
    element.dispatchEvent(new win.Event(element.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  }
  const chooseType = async (value) => { setInput(documentOf().querySelector("select"), value); await delay(40); };
  const edit = async (label, value) => { await click("Specify value", fieldset(label)); setInput(fieldset(label).querySelector("textarea"), value); await delay(30); };
  const wake = async () => { frame.contentWindow.dispatchEvent(new frame.contentWindow.Event("focus")); await delay(120); };
  const command = async (action) => { send(action); await delay(70); await wake(); };
  const navigate = async (nextScenario) => {
    scenario = nextScenario;
    stage = `navigating to ${scenario}`;
    const previous = documentOf();
    frame.src = `/specification-review?scenario=${scenario}`;
    await waitFor(() => {
      const current = documentOf();
      return current && current !== previous && current.readyState === "complete"
        && new URL(current.location.href).searchParams.get("scenario") === scenario
        && text().includes("Specification fields") && fieldset("Decking");
    }, `${scenario} workspace`);
    await waitFor(() => documentOf()?.querySelector('iframe[title="Current specification PDF preview"]'), "PDF loaded");
    await delay(200);
  };
  const save = async (expectedStatus = "Regenerating PDF") => { await click("Save corrections and regenerate PDF"); await waitFor(() => text().includes(expectedStatus) && !text().includes("Working"), expectedStatus); };
  const publish = async () => { await command("publish"); await waitFor(() => text().includes("Awaiting final review"), "published PDF state"); await waitFor(() => documentOf().querySelector('iframe[title="Current specification PDF preview"]'), "published preview loaded"); };
  const acceptAll = async () => { for (const label of labels) { const use = button("Use recommendation", fieldset(label)); if (use && !use.disabled) { use.click(); await delay(20); } } };
  try {
    await navigate("new_enquiry");
    assert(labels.every((label) => fieldset(label)), "All 18 specification fields render without a Task/chat page");
    assert(button("Mark specification complete").disabled, "Completion requires saved classification and reviewed fields");
    for (const label of labels) assert(["Use recommendation", "Specify value", "Specify blank", "Restore recommendation"].every((action) => button(action, fieldset(label))), `${label} exposes all four distinct field actions`);
    await chooseType("new_enquiry");
    for (const label of labels) await edit(label, `Reviewed ${label} — café`);
    const textareas = Array.from(documentOf().querySelectorAll("textarea"));
    assert(textareas.length === 18 && textareas.every((element) => element.labels.length > 0), "All field editors have associated labels and preserve Unicode input");
    assert(button("Mark specification complete").disabled, "Unsaved all-field corrections disable completion");
    await save();
    const saved = latestRequests().filter((request) => request.path.endsWith("/decisions")).at(-1);
    assert(saved && Object.keys(saved.body.field_operations).length === 18, "All 18 corrections are sent in one atomic save");
    assert(saved.body.field_operations["Incoming Information"].display_value === "Reviewed Incoming Information — café" && saved.body.field_operations.Revision.operation === "specify_value", "Incoming Information and Revision support authoritative reviewer values");
    assert(saved.body.expected_review_version === 0 && saved.body.expected_source_fingerprint && saved.body.idempotency_key, "Save carries review/source compare-and-swap and idempotency identities");
    assert(!saved.body.actor && !saved.body.provenance && saved.hasAuthorization, "Actor/provenance are server-authored and bearer travels in the header");
    assert(button("Mark specification complete").disabled, "Pending regeneration disables completion");
    await publish();
    assert(labels.every((label) => fieldset(label).textContent.includes("Source: Reviewer Specified")), "All saved overrides display Reviewer Specified provenance");
    const check = documentOf().querySelector('input[type="checkbox"]');
    assert(check && !check.disabled, "Current published preview enables final-check acknowledgement"); check.click(); await delay(40);
    await click("Mark specification complete"); await waitFor(() => text().includes("Specification complete. The item can now be moved"), "exact-PDF approval");
    const approved = latestRequests().filter((request) => request.path.endsWith("/approve")).at(-1);
    assert(Boolean(approved?.body.artifact_revision && approved.body.expected_decision_digest && approved.body.expected_publication_identity), "Approval identifies the exact published PDF, decisions and publication");
    await click("Restore recommendation", fieldset("Revision")); await save(); await publish();
    const restored = latestRequests().filter((request) => request.path.endsWith("/decisions")).at(-1);
    assert(restored.body.field_operations.Revision.operation === "restore_recommendation" && fieldset("Revision").textContent.includes("Source: Not assigned"), "Restoring removes a reviewer override and reinstates the unassigned new-enquiry recommendation");

    await navigate("amendment");
    assert(text().includes("AB17221_25.01 - A") && text().includes("Email company Riverside School differs"), "Detected reference and email/Account disagreement are visible");
    await chooseType("amendment");
    documentOf().querySelector('input[type="radio"][value="200:201"]').click(); await delay(40);
    documentOf().querySelector('input[type="radio"][value="300:301"]').click(); await delay(40);
    await acceptAll();
    await click("Specify blank", fieldset("Incoming Information"));
    await click("Restore recommendation", fieldset("Revision"));
    // Reaccept the restored recommendation so every populated amendment field is reviewed.
    await click("Use recommendation", fieldset("Revision"));
    await save(); await publish();
    const amendmentSave = latestRequests().filter((request) => request.path.endsWith("/decisions")).at(-1);
    assert(amendmentSave.body.selections.selected_project.item_id === "201" && amendmentSave.body.selections.selected_drawing.item_id === "301", "Amendment saves the internally selected project and drawing");
    assert(amendmentSave.body.field_operations["Incoming Information"].operation === "specify_blank" && amendmentSave.body.field_operations.Decking.operation === "accept_recommendation", "Intentional blank and accepted recommendation remain distinct commands");
    assert(fieldset("Decking").textContent.includes("Source: Monday CRM recommendation"), "Accepted CRM recommendation keeps CRM provenance");

    await edit("Decking", "Draft survives concurrent save");
    await command("concurrent_save");
    await click("Save corrections and regenerate PDF");
    await waitFor(() => text().includes("Load latest context and keep my edits"), "concurrent-review conflict");
    assert(fieldset("Decking").querySelector("textarea").value === "Draft survives concurrent save", "Concurrent review conflict preserves unsaved text");
    await click("Load latest context and keep my edits");
    assert(fieldset("Decking").querySelector("textarea").value === "Draft survives concurrent save", "Loading latest context keeps manual draft for reconciliation");
    await save();

    await edit("Company", "Draft survives source change");
    await command("sources_change");
    await waitFor(() => text().includes("Load latest context and keep my edits"), "source-change conflict");
    assert(fieldset("Company").querySelector("textarea").value === "Draft survives source change", "Polling source changes never overwrite draft text");
    await click("Load latest context and keep my edits"); await save();

    await edit("Contact", "Draft survives session expiry");
    await command("session_expiry");
    await waitFor(() => button("Reconnect review"), "expired session reconnect");
    assert(fieldset("Contact").querySelector("textarea").value === "Draft survives session expiry", "Session expiry preserves unsaved text in memory");
    await click("Reconnect review");
    await waitFor(() => !button("Reconnect review") && !button("Save corrections and regenerate PDF").disabled, "session resumed");
    assert(fieldset("Contact").querySelector("textarea").value === "Draft survives session expiry", "SDK rebootstrap restores access without losing draft"); await save();

    await edit("Surveyor", "Discard only on item switch");
    await command("switch_item");
    await waitFor(() => text().includes("Item 102") && text().includes("Northgate Library roof"), "new Monday item context");
    assert(!text().includes("Discard only on item switch") && !documentOf().querySelector("textarea"), "Context switch clears old item credentials and draft state");
    const requests = latestRequests();
    assert(requests.some((request) => request.path.endsWith("/session") && request.body.context.itemId === "102") && requests.some((request) => request.path.includes("/items/100/102")), "New item context bootstraps a new scoped API session");
    await command("leave_landing_zone");
    await waitFor(() => text().includes("Read-only; retained PDF available"), "early group move read-only");
    assert(button("Save corrections and regenerate PDF").disabled && button("Mark specification complete").disabled && documentOf().querySelector('iframe[title="Current specification PDF preview"]'), "Leaving Landing Zone disables mutations and retains authenticated PDF");

    await navigate("ambiguous");
    await chooseType("amendment"); documentOf().querySelector('input[type="radio"][value="200:201"]').click(); await delay(40);
    assert(text().includes("AB17221_25.01 - A") && text().includes("AB17221_25.02 - B") && text().includes("Several roofs are plausible"), "Ambiguous roof candidates remain separately reviewable");
    documentOf().querySelector('input[type="radio"][value="300:302"]').click(); await delay(40); await acceptAll();
    await command("regeneration_failure"); await save("Update failed; previous PDF available");
    assert(button("Mark specification complete").disabled && documentOf().querySelector('iframe[title="Current specification PDF preview"]'), "Regeneration failure keeps previous PDF and blocks completion");
    await click("Refresh recommendations"); await waitFor(() => text().includes("Regenerating PDF"), "retry queues PDF"); await publish();

    // Equivalent browser accessibility checks for this dedicated form surface.
    assert(Array.from(documentOf().querySelectorAll("input,select,textarea")).every((element) => element.labels.length > 0), "Every form control has a programmatically associated label");
    assert(Array.from(documentOf().querySelectorAll("iframe")).every((element) => element.title.trim()), "PDF preview has an accessible iframe title");
    const focusButton = button("Refresh recommendations"); focusButton.focus(); assert(documentOf().activeElement === focusButton, "Review actions are keyboard focusable");
    const oldWidth = frame.style.width; frame.style.width = "360px"; await delay(250);
    assert(documentOf().documentElement.scrollWidth <= frame.contentWindow.innerWidth + 2, "Workspace fits a 360px iframe without horizontal overflow"); frame.style.width = oldWidth;
    assert(!latestRequests().some((request) => /\/tasks|handoff|chat|change_column|move_item/.test(request.path)), "Browser journeys perform no chat ingestion or Monday business mutations");
    const result = { status: "passed", assertions: results.length, results };
    showResult(result); return result;
  } catch (error) {
    const result = { status: "failed", assertions: results.length, error: error.message,
      stack: error.stack, scenario, stage, documentUrl: documentOf()?.location.href,
      readyState: documentOf()?.readyState, results };
    showResult(result); return result;
  }
}
