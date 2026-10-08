"use strict";
// Human settings and review use DOM text only. No model tool can reach these controls.
(function () {
  let pastCount = null, modelState = null;
  const message = (text, bad = false) => { const node = $("settings-message"); node.textContent = text; node.className = bad ? "error" : "hint"; };
  const attempt = async (fn) => { try { await fn(); } catch (error) { message(error.message, true); } };
  const json = async (url, options) => (await api(url, options)).json();
  const body = (method, value) => ({method, body: JSON.stringify(value)});
  function feedback(node, id, saved) {
    if (node.querySelector(".feedback")) return;
    const row = el("div", "feedback");
    const up = el("button", "ghost", "👍"), down = el("button", "ghost", "👎"), clear = el("button", "ghost", "Clear vote");
    up.type = down.type = clear.type = "button";
    up.setAttribute("aria-label", "Helpful answer"); down.setAttribute("aria-label", "Unhelpful answer");
    const state = el("span", "hint", saved?.used_in_dataset ? "Used for training; vote locked" : "Not captured");
    const editor = el("div", "feedback-editor"); editor.hidden = true;
    const choice = el("select");
    for (const [value, label] of [["text", "What should it have said?"], ["tool", "Should have called a tool"]]) {
      const option = el("option", "", label); option.value = value; choice.append(option);
    }
    const correction = el("textarea", "correction"); correction.maxLength = 8000; correction.rows = 3;
    correction.value = saved?.correction || ""; correction.setAttribute("aria-label", "Correction");
    const tool = el("select"), args = el("textarea", "correction"); args.rows = 3; args.value = "{}";
    tool.hidden = args.hidden = true;
    const save = el("button", "ghost", "Save feedback"); save.type = "button";
    const errorNode = el("span", "error");
    choice.onchange = () => { correction.hidden = choice.value !== "text"; tool.hidden = args.hidden = choice.value !== "tool"; };
    let locked = !!saved?.used_in_dataset;
    const send = async (rating, extra = {}) => {
      up.disabled = down.disabled = save.disabled = true;
      try {
        const result = await json(`/api/messages/${id}/feedback`, body("POST", {rating, ...extra}));
        up.setAttribute("aria-pressed", String(rating === 1)); down.setAttribute("aria-pressed", String(rating === -1));
        state.textContent = result.captured ? "Included for training review" : "Not captured"; errorNode.textContent = "";
      } catch (error) { errorNode.textContent = error.message; if (error.status === 409) locked = true; }
      finally { up.disabled = down.disabled = clear.disabled = save.disabled = locked; }
    };
    up.onclick = () => send(1);
    down.onclick = async () => {
      editor.hidden = false; await send(-1);
      if (!tool.children.length) {
        try { for (const schema of await json("/api/feedback/tools")) { const option = el("option", "", schema.function.name); option.value = schema.function.name; tool.append(option); } }
        catch (error) { errorNode.textContent = error.message; }
      }
    };
    save.onclick = () => {
      try { return send(-1, choice.value === "tool" ? {correction_action: {action: "tool", tool: tool.value, args: JSON.parse(args.value)}} : {correction: correction.value || undefined}); }
      catch (error) { errorNode.textContent = error.message; }
    };
    clear.onclick = async () => {
      try { await api(`/api/messages/${id}/feedback`, {method: "DELETE"}); state.textContent = "Vote cleared"; editor.hidden = true; }
      catch (error) { errorNode.textContent = error.message; }
    };
    if (saved?.rating) { up.setAttribute("aria-pressed", String(saved.rating === 1)); down.setAttribute("aria-pressed", String(saved.rating === -1)); }
    if (locked) up.disabled = down.disabled = clear.disabled = save.disabled = true;
    editor.append(choice, correction, tool, args, save);
    row.append(up, down, clear, state, editor, errorNode); node.append(row);
  }
  async function chatTraining(id) {
    try {
      const state = await json(`/api/chats/${id}/training`);
      if (currentChat !== id) return;
      $("chat-training").hidden = false; $("chat-training-mode").value = state.mode;
      $("chat-training-state").textContent = state.capture ? "Capture is on" : "Capture is off";
    } catch (_) { $("chat-training").hidden = true; }
  }
  $("chat-training-mode").onchange = () => attempt(async () => {
    if (currentChat) { await json(`/api/chats/${currentChat}/training`, body("POST", {mode: $("chat-training-mode").value})); await chatTraining(currentChat); }
  });
  function personaBody() { return {agent_name: $("persona-name").value, persona: $("persona-tone").value, instructions: $("persona-instructions").value, confirm_over_budget: $("persona-confirm").checked}; }
  function preview(result) { $("persona-preview").textContent = result.preview; $("persona-tokens").textContent = `${result.token_count} tokens / ${result.budget} token budget${result.over_budget ? ". Confirmation required; longer prompt costs more time." : ""}`; }
  async function persona() {
    const result = await json("/api/settings/persona");
    $("persona-name").value = result.active.agent_name; $("persona-tone").value = result.active.persona; $("persona-instructions").value = result.active.instructions;
    $("persona-confirm").checked = false; preview(result);
    $("persona-history").replaceChildren();
    for (const version of await json("/api/settings/persona/versions")) {
      const option = el("option", "", `Version ${version.id} · ${fmtTime(version.created)}${version.active ? " · active" : ""}`); option.value = String(version.id); $("persona-history").append(option);
    }
  }
  $("persona-preview-button").onclick = () => attempt(async () => preview(await json("/api/settings/persona/preview", body("POST", personaBody()))));
  $("persona-form").onsubmit = (event) => { event.preventDefault(); attempt(async () => { await json("/api/settings/persona", body("PUT", personaBody())); await persona(); await loadStatus(); message("Persona saved as a new version."); }); };
  $("persona-history").onchange = () => attempt(async () => { const row = await json(`/api/settings/persona/versions/${$("persona-history").value}`); $("persona-diff").textContent = row.diff || "Same as the active version."; });
  $("persona-restore").onclick = () => attempt(async () => { await json(`/api/settings/persona/versions/${$("persona-history").value}/restore`, body("POST", {confirm_over_budget: $("persona-confirm").checked})); await persona(); message("Restored as a new version."); });
  async function examples() {
    const query = new URLSearchParams();
    for (const key of ["source", "included", "tainted"]) { const value = $("training-filter-" + key).value; if (value) query.set(key, value); }
    $("training-examples").replaceChildren();
    for (const row of await json("/api/training/examples?" + query)) {
      const card = el("details", "card"); card.append(el("summary", "", `${row.source} · ${fmtTime(row.created)} · ${row.tainted ? "tainted" : "clean source"} · ${row.include ? "included" : "excluded"}`));
      card.append(el("pre", "prompt-preview", JSON.stringify({messages: JSON.parse(row.messages_json), tools: JSON.parse(row.tools_json), output: JSON.parse(row.output_json)}, null, 2)));
      const target = el("textarea", "correction"); target.value = row.target_json || ""; target.maxLength = 8000;
      const toggle = el("button", "ghost", row.include ? "Exclude" : "Include after review"), save = el("button", "ghost", "Save target"), remove = el("button", "ghost danger", "Delete");
      toggle.onclick = () => attempt(async () => { await json(`/api/training/examples/${row.id}`, body("PATCH", {include: !row.include})); await examples(); });
      save.onclick = () => attempt(async () => { await json(`/api/training/examples/${row.id}`, body("PATCH", {target_json: JSON.parse(target.value)})); await examples(); });
      remove.onclick = () => attempt(async () => { await api(`/api/training/examples/${row.id}`, {method: "DELETE"}); await examples(); });
      if (row.used_in_dataset) toggle.disabled = save.disabled = remove.disabled = target.disabled = true;
      card.append(target, toggle, save, remove); $("training-examples").append(card);
    }
  }
  async function datasets() {
    $("training-datasets").replaceChildren();
    for (const row of await json("/api/training/datasets")) {
      const card = el("div", "card"), link = el("a", "button ghost", "Download dataset and training scripts");
      link.href = `/api/training/datasets/${encodeURIComponent(row.id)}/download`; link.download = row.id + ".tar.gz";
      const start = el("button", "ghost", "Start training run"); start.onclick = () => attempt(async () => { const run = await json("/api/training/runs", body("POST", {dataset_id: row.id})); message(run.mode === "manual" ? "Dataset ready. Download it and follow training/README.md on your separate GPU machine." : "Remote run started."); await model(); });
      card.append(el("strong", "", row.id), el("p", "hint", `${row.n_sft} SFT · ${row.n_dpo} pairs · ${row.n_eval} private eval · ${row.n_seed} seed`), link, start); $("training-datasets").append(card);
    }
  }
  $("training-refresh").onclick = () => attempt(examples);
  $("training-save").onclick = () => attempt(async () => { await json("/api/settings/training", body("PUT", {capture: $("training-capture").checked, loop_enabled: $("training-loop").checked})); message("Capture settings saved. Past chats are not included automatically."); if (currentChat) chatTraining(currentChat); });
  $("training-export").onclick = () => attempt(async () => { await json("/api/training/datasets", body("POST", {})); await datasets(); await examples(); message("Scrubbed dataset built. Review it before transfer."); });
  $("training-past").onclick = () => attempt(async () => { const count = await json("/api/training/past-count"); pastCount = count.count; $("training-past-count").textContent = `${count.count} past labelled replies. ${count.note}`; $("training-past-confirm").hidden = false; });
  $("training-past-include").onclick = () => attempt(async () => { if (pastCount === null) return; await json("/api/training/datasets", body("POST", {include_past: true, confirmed_count: pastCount})); pastCount = null; $("training-past-confirm").hidden = true; await datasets(); });
  function twoStep(button, perform, label) {
    let started = null;
    button.onclick = () => attempt(async () => {
      if (started === null || Date.now() - started > 300000) { await perform(false); started = Date.now(); button.textContent = "Confirm " + label; return; }
      if (Date.now() - started < 1000) return;
      button.disabled = true;
      try { await perform(true); started = null; await model(); await loadStatus(); } finally { button.disabled = false; }
    });
  }
  async function promotions(container) {
    container.replaceChildren();
    const state = modelState || await json("/api/models");
    for (const request of await json("/api/models/promotions")) {
      const version = state.versions[request.version_id]; if (!version) continue;
      const card = el("div", "card"); card.append(el("strong", "", request.version_id), el("p", "hint", "Retraining can amplify errors, forget skills or learn injected instructions. Review this model and comparison before promoting it."));
      card.append(el("pre", "prompt-preview", JSON.stringify(version.comparison || version.manifest.eval_summary, null, 2)), el("pre", "prompt-preview", version.model_card));
      const typed = el("input"); typed.placeholder = "Type the exact version id"; typed.setAttribute("aria-label", "Confirm model version id");
      const promote = el("button", "primary", "Promote"), discard = el("button", "ghost danger", "Discard");
      twoStep(promote, (confirm) => { if (typed.value !== request.version_id) throw new Error("Type the exact version id first."); return json(`/api/models/promotions/${request.id}/promote`, body("POST", {version_id: typed.value, sha256: version.sha256, confirm})); }, "promotion");
      discard.onclick = () => attempt(async () => { await json(`/api/models/promotions/${request.id}/discard`, body("POST", {})); await model(); });
      card.append(typed, promote, discard); container.append(card);
    }
  }
  async function model() {
    modelState = await json("/api/models"); $("model-current").textContent = `Current: ${modelState.current || "not installed"} · Previous: ${modelState.previous || "none"}${modelState.trainer_off ? ". Trainer off; use the documented host commands." : ""}`;
    $("model-versions").replaceChildren();
    const protectedIds = new Set([modelState.current, modelState.previous, ...Object.keys(modelState.versions).filter(id => id.endsWith("-base"))]);
    for (const [id, row] of Object.entries(modelState.versions)) {
      const card = el("details", "card"); card.append(el("summary", "", `${id} · ${row.status}`), el("pre", "prompt-preview", row.model_card));
      if (!protectedIds.has(id)) { const remove = el("button", "ghost danger", "Review removal of this version"); twoStep(remove, (confirm) => json("/api/models/prune", body("POST", {confirmed_ids: [id], confirm})), "removal"); card.append(remove); }
      $("model-versions").append(card);
    }
    $("model-rollback").hidden = !modelState.previous;
    if (modelState.previous) twoStep($("model-rollback"), (confirm) => json("/api/models/rollback", body("POST", {to_version_id: modelState.previous, confirm})), "rollback");
    await promotions($("model-promotions")); $("training-runs").replaceChildren();
    for (const row of await json("/api/training/runs")) { const card = el("div", "card"); card.append(el("strong", "", `${row.id} · ${row.status}`), el("p", "hint", row.error || row.mode)); if (["running", "launching", "waiting_manual"].includes(row.status)) { const cancel = el("button", "ghost danger", "Cancel"); cancel.onclick = () => attempt(async () => { await json(`/api/training/runs/${row.id}/cancel`, body("POST", {})); await model(); }); card.append(cancel); } $("training-runs").append(card); }
  }
  $("model-import-button").onclick = () => attempt(async () => {
    const file = $("model-import").files[0]; if (!file) throw new Error("Choose candidate.tar first.");
    const response = await fetch("/api/models/import", {method: "PUT", credentials: "same-origin", headers: {"X-CSRF-Token": csrf, "Content-Type": "application/x-tar"}, body: file});
    if (!response.ok) throw new Error((await response.json()).detail || "Import failed");
    message("Candidate imported for review. It has not been promoted."); await model();
  });
  async function settings() {
    message("");
    await attempt(persona);
    await attempt(async () => { const state = await json("/api/settings/training"); $("training-capture").checked = state.capture; $("training-loop").checked = state.loop_enabled; $("training-schedule").textContent = `${state.schedule} · ${state.mode}${state.blocked ? " · paused for a sign-in or screen session" : ""}`; });
    await attempt(examples); await attempt(datasets); await attempt(model);
  }
  $("tab-settings").onclick = () => showView("settings");
  async function systemJob(container) {
    const state = await json("/api/jobs/training");
    const card = el("div", "card job"), toggle = el("button", "ghost", state.loop_enabled ? "Pause training job" : "Enable training job");
    card.append(el("strong", "", "Prepare training candidate · system job"), el("p", "hint", `${state.schedule} · ${state.mode} · ${state.loop_enabled ? "enabled" : "paused"}. A model always waits for your promotion approval.`));
    if (state.notice) card.append(el("p", "hint", JSON.stringify(state.notice)));
    toggle.onclick = () => attempt(async () => { await json("/api/jobs/training/toggle", {method: "POST"}); await loadJobs(); });
    card.append(toggle); container.append(card);
  }
  window.m8 = {feedback, chatTraining, settings, promotions, systemJob};
})();
