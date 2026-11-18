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
  const button = (label, root) => buttons(root).find((element) => element.textContent.trim() === label || element.getAttribute("aria-label") === label);
  const click = async (label, root) => { stage = `clicking ${label}`; const element = button(label, root); if (!element || element.disabled) throw new Error(`Unavailable button: ${label}`); element.click(); await delay(30); };
  const fieldset = (label) => Array.from(documentOf()?.querySelectorAll("fieldset") || []).find((element) => element.querySelector("legend")?.textContent.trim() === label);
  const sourceSections = () => Array.from(documentOf()?.querySelectorAll("section") || []).filter((element) => element.querySelector(":scope > h3")?.textContent.trim() === "Source needs review");
  const sourceChecks = () => sourceSections().map((element) => element.querySelector('input[type="checkbox"]'));
  const canvas = () => documentOf()?.querySelector('[data-testid="specification-pdf-canvas"]');
  const renderedCanvas = (page) => {
    const element = canvas();
    return element?.dataset.rendered === "true" && (!page || element.getAttribute("aria-label") === `Specification PDF page ${page} of 2`) ? element : null;
  };
  const previewHref = () => Array.from(documentOf()?.querySelectorAll("a") || []).find((element) => element.textContent.trim() === "Open PDF in new tab")?.href;
  const pixels = () => canvas()?.getContext("2d").getImageData(0, 0, canvas().width, canvas().height).data;
  const hasInk = () => {
    const data = pixels();
    for (let index = 0; data && index < data.length; index += 4) {
      if (data[index + 3] > 0 && (data[index] < 220 || data[index + 1] < 220 || data[index + 2] < 220)) return true;
    }
    return false;
  };
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
  const navigate = async (nextScenario, expectPreview = true) => {
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
    if (expectPreview) await waitFor(() => renderedCanvas(), "PDF painted on canvas");
    await delay(200);
  };
  const save = async (expectedStatus = "Regenerating PDF") => { await waitFor(() => { const action = button("Save corrections and regenerate PDF"); return action && !action.disabled; }, "save corrections available"); await click("Save corrections and regenerate PDF"); await waitFor(() => text().includes(expectedStatus) && !text().includes("Working"), expectedStatus); };
  const publish = async () => {
    const previousHref = previewHref();
    await command("publish");
    await waitFor(() => text().includes("Awaiting final review"), "published PDF state");
    await waitFor(() => previewHref() && previewHref() !== previousHref && renderedCanvas(1), "new published revision painted");
  };
  const acceptAll = async () => { for (const label of labels) { const use = button("Use recommendation", fieldset(label)); if (use && !use.disabled) { use.click(); await delay(20); } } };
  try {
    await navigate("new_enquiry");
    assert(hasInk() && !documentOf().querySelector('iframe[title="Current specification PDF preview"]'), "PDF pages render real pixels without a nested native PDF iframe");
    assert(renderedCanvas(1) && text().includes("Page 1 of 2") && button("Previous page").disabled && !button("Next page").disabled, "PDF preview starts on the first page with accessible page controls");
    const firstPage = canvas().toDataURL();
    await click("Next page");
    await waitFor(() => renderedCanvas(2) && canvas().toDataURL() !== firstPage, "second PDF page painted");
    assert(hasInk() && button("Next page").disabled && !button("Previous page").disabled, "Page navigation paints different content and respects document bounds");
    await click("Previous page");
    await waitFor(() => renderedCanvas(1) && canvas().toDataURL() === firstPage, "first PDF page restored");
    const originalCanvasWidth = canvas().width;
    await click("Zoom in");
    await waitFor(() => renderedCanvas(1) && canvas().width > originalCanvasWidth, "zoomed PDF painted");
    assert(hasInk(), "Zoom changes the rendered PDF resolution while keeping document content");
    await click("Zoom out");
    await waitFor(() => renderedCanvas(1) && canvas().width === originalCanvasWidth, "fit-width PDF restored");

    await navigate("pdf_error", false);
    await waitFor(() => text().includes("The PDF preview could not be rendered."), "invalid PDF rendering error");
    assert(previewHref()?.startsWith("blob:") && Array.from(documentOf().querySelectorAll("a")).some((element) => element.download && element.textContent.trim() === "Download PDF"), "Render failure retains protected Open and Download links");
    assert(!renderedCanvas() && documentOf().querySelector('input[type="checkbox"]').disabled, "An unrendered PDF cannot enable final-check acknowledgement");
    const failedFetches = latestRequests().filter((request) => request.path.includes("/pdf/")).length;
    await click("Retry preview");
    await waitFor(() => renderedCanvas(1), "valid PDF rendered after retry");
    assert(hasInk() && latestRequests().filter((request) => request.path.includes("/pdf/")).length > failedFetches, "Preview retry refetches the protected PDF and recovers from rendering failure");

    await navigate("new_enquiry");
    assert(labels.every((label) => fieldset(label)), "All 18 specification fields render without a Task/chat page");
    await command("analysis_pending");
    await waitFor(() => text().includes("Validated fields available"), "early validated field preview");
    assert(labels.every((label) => fieldset(label)), "Validated extraction fields remain visible while project matching is pending");
    assert(text().includes("Matching projects and drawings") && text().includes("8 of 8 sources processed"), "Review shows the recorded matching stage and source counts");
    assert(button("Specify value", fieldset("Decking")).disabled && button("Save corrections and regenerate PDF").disabled && button("Mark specification complete").disabled, "Provisional extraction is read-only and cannot save or complete");
    await command("analysis_rendering");
    await waitFor(() => text().includes("Preparing design documents"), "recorded render progress update");
    assert(labels.every((label) => fieldset(label)), "A lightweight progress update preserves all displayed fields");
    await command("analysis_complete");
    await waitFor(() => !button("Specify value", fieldset("Decking")).disabled, "full analysis enables review controls");
    await waitFor(() => renderedCanvas(1), "completed analysis preview painted");
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
    await click("Mark specification complete"); await waitFor(() => text().includes("The current published specification has been marked complete."), "exact-PDF approval");
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
    await waitFor(() => renderedCanvas(), "previous item PDF painted");
    await click("Next page");
    await waitFor(() => renderedCanvas(2), "previous item second page painted");
    const previousItemHref = previewHref();
    const previousItemPixels = canvas().toDataURL();
    await command("switch_item");
    await waitFor(() => text().includes("Item 102") && text().includes("Northgate Library roof"), "new Monday item context");
    await waitFor(() => renderedCanvas(1) && previewHref() !== previousItemHref, "new item PDF painted from first page");
    assert(hasInk() && canvas().toDataURL() !== previousItemPixels, "Switching items discards the previous document and resets the current page");
    assert(!text().includes("Discard only on item switch") && !documentOf().querySelector("textarea"), "Context switch clears old item credentials and draft state");
    const requests = latestRequests();
    assert(requests.some((request) => request.path.endsWith("/session") && request.body.context.itemId === "102") && requests.some((request) => request.path.includes("/items/100/102")), "New item context bootstraps a new scoped API session");
    await command("leave_landing_zone");
    await waitFor(() => button("Save corrections"), "active group manual publication");
    assert(button("Update Published PDF") && !button("Specify value", fieldset("Decking")).disabled, "Moving to active groups preserves editing and separates publication");
    await command("complete_group");
    await waitFor(() => text().includes("Completed Folder"), "completed group read-only");
    await waitFor(() => renderedCanvas(), "retained completed-item PDF painted");
    assert(button("Mark specification complete").disabled && hasInk(), "Completed Folder disables mutations and retains authenticated PDF");

    await navigate("active");
    await waitFor(() => button("Publish Specification PDF") && !button("Publish Specification PDF").disabled, "initial draft ready");
    assert(button("Save corrections").disabled, "First publication does not require artificial corrections");
    assert(!latestRequests().some((request) => request.path.endsWith("/publish")), "Opening active review prepares recommendations without publishing");
    await click("Refresh recommendations");
    await waitFor(() => !button("Publish Specification PDF").disabled, "refreshed draft ready");
    assert(!latestRequests().some((request) => request.path.endsWith("/publish")), "Refreshing active recommendations does not publish");
    await click("Publish Specification PDF");
    await waitFor(() => text().includes("Awaiting final review"), "explicit first publication");
    assert(button("Update Published PDF").disabled, "Publication button changes after first successful publication");
    await command("sources_change");
    await waitFor(() => text().includes("Recommended publication changes") && !button("Update Published PDF").disabled, "source changes recommend replacement");
    assert(latestRequests().filter((request) => request.path.endsWith("/publish")).length === 1, "Later source changes do not automatically publish");
    await click("View published PDF");
    await waitFor(() => button("Update Published PDF").disabled, "published version inspection");
    await waitFor(() => renderedCanvas(1), "published document painted");
    const publishedHref = previewHref();
    await click("Next page");
    await waitFor(() => renderedCanvas(2), "published document second page painted");
    await click("View draft PDF");
    await waitFor(() => previewHref() !== publishedHref && renderedCanvas(1), "draft document painted from first page");
    assert(hasInk(), "Switching between published and draft revisions resets the rendered document");
    await edit("Decking", "Reviewed active deck");
    assert(button("Update Published PDF").disabled, "Unsaved corrections block publication");
    await click("Save corrections");
    await waitFor(() => !button("Update Published PDF").disabled, "saved draft ready");
    assert(latestRequests().filter((request) => request.path.endsWith("/publish")).length === 1, "Saving active corrections prepares a draft without replacing the PDF");
    await click("Update Published PDF");
    await waitFor(() => text().includes("Awaiting final review"), "explicit replacement publication");
    assert(latestRequests().filter((request) => request.path.endsWith("/publish")).length === 2, "Each active publication requires a separate explicit request");

    await navigate("source_reviews");
    assert(sourceSections().length === 3 && sourceChecks().every(Boolean), "Each flagged source has its own confirmation checkbox");
    assert(sourceSections().every((element) => !element.querySelector("textarea") && element.textContent.includes("I confirm this source issue has been addressed.")), "Source review requires a labelled checkbox without an explanation textbox");
    assert(sourceChecks()[0].checked && !sourceChecks()[1].checked && !sourceChecks()[2].checked, "A current legacy review remains checked while unreviewed sources need confirmation");
    await chooseType("new_enquiry"); await acceptAll();
    sourceChecks()[1].click(); sourceChecks()[2].click(); await delay(40);
    await save();
    const confirmedSources = latestRequests().filter((request) => request.path.endsWith("/decisions")).at(-1).body.source_dispositions;
    assert(confirmedSources.length === 2 && confirmedSources.every((entry) => entry.addressed === true && !("explanation" in entry)), "Checking source warnings saves boolean confirmations without written explanations");
    await waitFor(() => sourceChecks().every((element) => element.checked), "saved source confirmations retained");
    assert(sourceSections().length === 3, "Confirmed source warnings remain visible for reference");
    sourceChecks()[1].click(); await delay(40); await save();
    const revokedSource = latestRequests().filter((request) => request.path.endsWith("/decisions")).at(-1).body.source_dispositions;
    assert(revokedSource.length === 1 && revokedSource[0].addressed === false, "Unticking a saved source confirmation sends an explicit revocation");
    await waitFor(() => !sourceChecks()[1].checked, "revoked source remains unchecked");
    assert(text().includes("Review source issue: roof_scan.obj"), "A revoked source confirmation requires review again before completion");
    await command("sources_change");
    await waitFor(() => sourceChecks().every((element) => !element.checked), "changed sources require renewed confirmations");
    assert(text().includes("The sources have changed. Review this issue again before confirming it."), "Source changes explain why previous confirmations need renewal");
    await command("complete_group");
    await waitFor(() => sourceChecks().every((element) => element.disabled), "excluded item source controls are read-only");
    assert(sourceChecks().every((element) => element.labels.length > 0), "Source confirmation checkboxes have associated accessible labels");

    await navigate("many_projects");
    const extractedTitle = () => fieldset("Project candidates").querySelector("dl");
    assert(extractedTitle()?.querySelector("dt")?.textContent === "Extracted Project Title"
      && extractedTitle().querySelector("dd")?.textContent === "Riverside School", "Project candidates show the extracted title separately from matched project titles");
    const projectChoices = () => Array.from(fieldset("Project candidates").querySelectorAll('input[type="radio"]')).filter((element) => element.value && element.getClientRects().length > 0);
    const warningCopies = (message) => Array.from(documentOf().querySelectorAll("li")).filter((element) => element.textContent.trim() === message).length;
    assert(warningCopies("Select the relevant roof/drawing; several series may be plausible") === 1, "Duplicate drawing warnings from legacy candidate and intent lists appear only once");
    assert(warningCopies("Confirm the intended request type with the client.") === 1 && text().includes("Narrow the project candidates to read further drawing details"), "Distinct candidate warnings and deduplicated request reasons remain visible");
    assert(projectChoices().length === 10 && !documentOf().querySelector('input[type="radio"][value="200:213"]'), "More than ten project matches initially shows only the first ten choices");
    const candidateLabel = (index) => projectChoices()[index].labels[0].textContent;
    assert(["88.5%", "43.2%", "42.7%"].every((score, index) => candidateLabel(index).includes(`Title similarity: ${score}`)), "Each scored project shows its own title similarity percentage");
    assert(!candidateLabel(3).includes("Title similarity") && candidateLabel(4).includes("Title similarity: 0.0%"), "Unavailable similarity scores are omitted and zero remains visible");
    assert(candidateLabel(5).includes("Title similarity: 56.2%"), "Title similarity rounds halfway scores the same way as the matched-projects PDF");
    const expandProjects = button("Show all 13 projects");
    assert(expandProjects?.getAttribute("aria-expanded") === "false" && documentOf().getElementById(expandProjects.getAttribute("aria-controls")), "Project disclosure exposes its collapsed state and controlled list to assistive technology");
    await click("Show all 13 projects");
    assert(projectChoices().length === 13 && button("Show fewer projects")?.getAttribute("aria-expanded") === "true", "Expanding project matches reveals every candidate and updates disclosure state");
    documentOf().querySelector('input[type="radio"][value="200:213"]').click(); await delay(40);
    await click("Show fewer projects");
    assert(projectChoices().length === 11 && documentOf().querySelector('input[type="radio"][value="200:213"]')?.checked, "Folding retains a selected project outside the first ten matches");
    assert(extractedTitle()?.querySelector("dd")?.textContent === "Riverside School", "Choosing and folding a matched project preserves the original extracted title");
    assert(!documentOf().querySelector('input[type="radio"][value="200:211"]') && button("Show all 13 projects")?.getAttribute("aria-expanded") === "false", "Folding hides other additional matches and restores the collapsed disclosure");
    await click("Show all 13 projects");
    assert(projectChoices().length === 13 && documentOf().querySelector('input[type="radio"][value="200:213"]')?.checked, "Expanding again preserves the selected project");

    await navigate("ten_projects");
    assert(extractedTitle()?.querySelector("dd")?.textContent === "Not available from the current extraction.", "Missing extracted titles have an explicit fallback without substituting a matched project or Drawing Title");
    assert(projectChoices().length === 10 && !buttons(fieldset("Project candidates")).some((element) => /Show all|Show fewer/.test(element.textContent)), "Exactly ten matched projects stays fully visible without a fold control");

    await navigate("ambiguous");
    await chooseType("amendment"); documentOf().querySelector('input[type="radio"][value="200:201"]').click(); await delay(40);
    assert(text().includes("AB17221_25.01 - A") && text().includes("AB17221_25.02 - B") && text().includes("Several roofs are plausible"), "Ambiguous roof candidates remain separately reviewable");
    documentOf().querySelector('input[type="radio"][value="300:302"]').click(); await delay(40); await acceptAll();
    await command("regeneration_failure"); await save("Update failed; previous PDF available");
    await waitFor(() => renderedCanvas(), "previous PDF remains painted after regeneration failure");
    assert(button("Mark specification complete").disabled && hasInk(), "Regeneration failure keeps previous PDF and blocks completion");
    await click("Refresh recommendations"); await waitFor(() => text().includes("Regenerating PDF"), "retry queues PDF"); await publish();

    // Equivalent browser accessibility checks for this dedicated form surface.
    assert(Array.from(documentOf().querySelectorAll("input,select,textarea")).every((element) => element.labels.length > 0), "Every form control has a programmatically associated label");
    assert(canvas()?.getAttribute("role") === "img" && /^Specification PDF page \d+ of \d+$/.test(canvas().getAttribute("aria-label")), "Rendered PDF page has an accessible image role and page label");
    const focusButton = button("Refresh recommendations"); focusButton.focus(); assert(documentOf().activeElement === focusButton, "Review actions are keyboard focusable");
    const oldWidth = frame.style.width; const wideCanvasWidth = canvas().width; frame.style.width = "360px";
    await waitFor(() => renderedCanvas() && canvas().width < wideCanvasWidth, "PDF resized for narrow iframe");
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
