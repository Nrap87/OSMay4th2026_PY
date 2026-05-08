const $ = (id) => document.getElementById(id);

async function parseJsonRes(res) {
  const text = await res.text();
  try {
    return { ok: res.ok, status: res.status, body: JSON.parse(text) };
  } catch {
    return { ok: res.ok, status: res.status, body: text };
  }
}

function showModal(title, text) {
  $("modal-title").textContent = title;
  $("modal-body").textContent = text;
  $("modal").classList.remove("hidden");
}

function hideModal() {
  $("modal").classList.add("hidden");
}

async function loadStatus() {
  const res = await fetch("/api/outsystems/status");
  const { ok, body } = await parseJsonRes(res);
  const el = $("status-line");
  if (!ok) {
    el.textContent = "Status error: " + (typeof body === "string" ? body : JSON.stringify(body));
    return;
  }
  if (!body.configured) {
    el.textContent =
      "Not configured — enter PlayerGuid + PlayerEmail and Save, or set PLAYER_GUID / PLAYER_EMAIL on the server.";
    return;
  }
  el.textContent =
    `Configured (${body.source})  |  Guid: ${body.player_guid_masked ?? "—"}  |  Email: ${body.player_email_masked ?? "—"}  |  Host: ${body.base_url}`;
}

async function saveCreds(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const payload = {
    player_guid: String(fd.get("player_guid") || "").trim(),
    player_email: String(fd.get("player_email") || "").trim(),
  };
  const bu = String(fd.get("base_url") || "").trim();
  if (bu) payload.base_url = bu;
  const res = await fetch("/api/outsystems/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const { ok, body } = await parseJsonRes(res);
  if (!ok) {
    alert(typeof body.detail === "string" ? body.detail : JSON.stringify(body));
    return;
  }
  await loadStatus();
}

async function clearSession() {
  await fetch("/api/outsystems/session", { method: "DELETE" });
  await loadStatus();
}

async function refreshMap() {
  const msg = $("refresh-msg");
  msg.textContent = "Loading…";
  msg.classList.remove("err");
  const res = await fetch("/api/outsystems/refresh", { method: "POST" });
  const { ok, body } = await parseJsonRes(res);
  if (!ok) {
    msg.textContent =
      typeof body === "object" && body.detail
        ? body.detail
        : JSON.stringify(body);
    msg.classList.add("err");
    return;
  }
  msg.textContent = "Last refresh OK.";
  $("stats").classList.remove("hidden");
  $("stat-planets").textContent = body.planet_count;
  $("stat-routes").textContent = body.route_count;
  $("stat-challenges").textContent = body.challenge_count;
  renderCards(body.challenges || []);
}

function renderCards(challenges) {
  const root = $("cards");
  root.innerHTML = "";
  for (const c of challenges) {
    const id = c.challengeId;
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.cid = id != null ? String(id) : "";
    const finished = c.isFinished ? '<span class="tag done">Finished</span>' : "";
    const noId = id == null;
    card.innerHTML = `
      <h3>${escapeHtml(c.challengeName)}</h3>
      <p class="meta">
        ChallengeId: <strong>${id != null ? id : "—"}</strong><br/>
        Start ${c.startPlanetId} · mandatory ${c.mandatoryCount} · forbidden ${c.forbiddenCount} · bonuses ${c.bonusCount}
      </p>
      <div class="tags">${finished}</div>
      <div class="card-actions">
        <button type="button" class="btn-solve" data-submit="false" ${noId ? "disabled" : ""}>Solve</button>
        <button type="button" class="btn-solve primary" data-submit="true" ${noId ? "disabled" : ""}>Solve &amp; submit</button>
      </div>
    `;
    root.appendChild(card);
  }

  root.querySelectorAll(".btn-solve").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".card");
      const cid = card && card.dataset.cid;
      if (!cid) {
        alert("Missing ChallengeId");
        return;
      }
      runChallenge(Number(cid), btn.dataset.submit === "true", btn);
    });
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function runChallenge(challengeId, submit, btn) {
  const label = submit ? "Solve & submit" : "Solve";
  btn.disabled = true;
  try {
    const q = submit ? "?submit=true" : "?submit=false";
    const res = await fetch(
      `/api/outsystems/challenges/${challengeId}/run${q}`,
      { method: "POST" }
    );
    const { body } = await parseJsonRes(res);
    const text = JSON.stringify(body, null, 2);
    showModal(`${label} — Challenge ${challengeId}`, text);
  } catch (e) {
    showModal("Error", String(e));
  } finally {
    btn.disabled = false;
  }
}

async function submitAll() {
  const n = parseInt($("parallel-all").value, 10) || 6;
  if (
    !confirm(
      "Run ALL daily challenges: parallel solve + ordered submit (same as CLI --all-challenges --submit)? This may take a long time."
    )
  ) {
    return;
  }
  $("btn-submit-all").disabled = true;
  try {
    const res = await fetch("/api/outsystems/submit-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parallel: n }),
    });
    const { body } = await parseJsonRes(res);
    showModal("Submit all — result", JSON.stringify(body, null, 2));
  } catch (e) {
    showModal("Error", String(e));
  } finally {
    $("btn-submit-all").disabled = false;
  }
}

$("creds-form").addEventListener("submit", saveCreds);
$("btn-clear-session").addEventListener("click", clearSession);
$("btn-refresh").addEventListener("click", refreshMap);
$("btn-submit-all").addEventListener("click", submitAll);
$("modal-close").addEventListener("click", hideModal);
$("modal").addEventListener("click", (ev) => {
  if (ev.target === $("modal")) hideModal();
});

loadStatus();
