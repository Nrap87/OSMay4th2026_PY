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

function setRunLock(on) {
  document.body.classList.toggle("run-locked", on);
  document.querySelectorAll(".btn-solve, #btn-submit-all").forEach((b) => {
    b.disabled = on;
  });
}

function showRunOverlay(title) {
  $("run-overlay").classList.remove("hidden");
  $("run-overlay").setAttribute("aria-hidden", "false");
  $("run-overlay-title").textContent = title;
  $("run-overlay-status").textContent = "Running… output streams below.";
  $("run-log").textContent = "";
  $("run-dump").textContent = "";
  $("run-dump-wrap").classList.add("hidden");
  $("run-dismiss").disabled = true;
  $("run-spinner").classList.remove("hidden");
  setRunLock(true);
}

function scrollRunLog() {
  const el = $("run-log");
  el.scrollTop = el.scrollHeight;
}

function finishRunOverlay(ok, exitCode) {
  $("run-spinner").classList.add("hidden");
  $("run-dismiss").disabled = false;
  $("run-overlay-status").textContent = ok
    ? `Finished (exit code ${exitCode}). Review output, then Close.`
    : `Finished with errors (exit code ${exitCode}). Review output, then Close.`;
  scrollRunLog();
}

function hideRunOverlay() {
  $("run-overlay").classList.add("hidden");
  $("run-overlay").setAttribute("aria-hidden", "true");
  setRunLock(false);
}

/**
 * POST and read text/event-stream: JSON lines after "data: ".
 */
async function streamSolver(url, body) {
  const headers = { Accept: "text/event-stream" };
  if (body != null) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(url, {
    method: "POST",
    headers,
    body: body != null ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const t = await res.text();
    let detail = t;
    try {
      const j = JSON.parse(t);
      detail = j.detail != null ? JSON.stringify(j.detail) : t;
    } catch {
      /* keep text */
    }
    throw new Error(`${res.status} ${res.statusText}\n${detail}`);
  }

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let exitCode = null;
  let dump = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of block.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(6));
        } catch {
          $("run-log").textContent += `\n[parse error] ${line}\n`;
          continue;
        }
        if (ev.type === "line") {
          $("run-log").textContent += ev.text + "\n";
          scrollRunLog();
        } else if (ev.type === "error") {
          $("run-log").textContent += `\n[error] ${ev.message}\n`;
          scrollRunLog();
        } else if (ev.type === "done") {
          exitCode = ev.exit_code;
          dump = ev.dump;
        }
      }
    }
  }

  return { exitCode: exitCode ?? -1, dump };
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
      runChallengeStream(Number(cid), btn.dataset.submit === "true");
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

async function runChallengeStream(challengeId, submit) {
  const label = submit ? "Solve & submit" : "Solve";
  showRunOverlay(`${label} — Challenge ${challengeId}`);
  try {
    const q = submit ? "?submit=true" : "?submit=false";
    const { exitCode, dump } = await streamSolver(
      `/api/outsystems/challenges/${challengeId}/run/stream${q}`,
      null
    );
    if (dump != null) {
      $("run-dump-wrap").classList.remove("hidden");
      $("run-dump").textContent = JSON.stringify(dump, null, 2);
    }
    finishRunOverlay(exitCode === 0, exitCode);
  } catch (e) {
    $("run-log").textContent += `\n${String(e)}\n`;
    $("run-spinner").classList.add("hidden");
    $("run-dismiss").disabled = false;
    $("run-overlay-status").textContent = "Request failed — see log.";
  }
}

async function submitAllStream() {
  const n = parseInt($("parallel-all").value, 10) || 6;
  if (
    !confirm(
      "Run ALL daily challenges: parallel solve + ordered submit (same as CLI --all-challenges --submit)? This may take a long time."
    )
  ) {
    return;
  }
  showRunOverlay("Submit all challenges");
  try {
    const { exitCode } = await streamSolver("/api/outsystems/submit-all/stream", {
      parallel: n,
    });
    finishRunOverlay(exitCode === 0, exitCode);
  } catch (e) {
    $("run-log").textContent += `\n${String(e)}\n`;
    $("run-spinner").classList.add("hidden");
    $("run-dismiss").disabled = false;
    $("run-overlay-status").textContent = "Request failed — see log.";
  }
}

$("creds-form").addEventListener("submit", saveCreds);
$("btn-clear-session").addEventListener("click", clearSession);
$("btn-refresh").addEventListener("click", refreshMap);
$("btn-submit-all").addEventListener("click", submitAllStream);
$("modal-close").addEventListener("click", hideModal);
$("modal").addEventListener("click", (ev) => {
  if (ev.target === $("modal")) hideModal();
});
$("run-dismiss").addEventListener("click", hideRunOverlay);

loadStatus();
