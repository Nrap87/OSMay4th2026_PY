const $ = (id) => document.getElementById(id);

function showError(msg) {
  const el = $("err");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function hideError() {
  $("err").classList.add("hidden");
}

function showResults(data) {
  hideError();
  $("out").classList.remove("hidden");
  $("gross").textContent = data.gross_fuel.toFixed(2);
  $("bonus").textContent = data.bonus_subtotal.toFixed(2);
  $("net").textContent = data.net_fuel_solver_style.toFixed(2);
  $("cid").textContent =
    data.challenge_id != null ? String(data.challenge_id) : "—";
  $("validation").textContent = JSON.stringify(data.validation, null, 2);

  const tb = $("legs").querySelector("tbody");
  tb.innerHTML = "";
  for (const leg of data.legs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${leg.from}</td><td>${leg.to}</td><td>${leg.cost.toFixed(4)}</td>`;
    tb.appendChild(tr);
  }
}

async function score() {
  hideError();
  $("out").classList.add("hidden");

  let dataRaw;
  let routeRaw;
  let challengeRaw = $("challenge").value.trim();

  try {
    dataRaw = JSON.parse($("data").value || "{}");
  } catch (e) {
    showError("Map JSON: " + e.message);
    return;
  }
  try {
    routeRaw = JSON.parse($("route").value || "[]");
  } catch (e) {
    showError("Route JSON: " + e.message);
    return;
  }
  if (!Array.isArray(routeRaw)) {
    showError("Route must be a JSON array of planet ids.");
    return;
  }

  let challenge = null;
  if (challengeRaw) {
    try {
      challenge = JSON.parse(challengeRaw);
    } catch (e) {
      showError("Challenge JSON: " + e.message);
      return;
    }
  }

  const body = { data: dataRaw, challenge, route: routeRaw };

  let res;
  try {
    res = await fetch("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    showError("Network: " + e.message);
    return;
  }

  const text = await res.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    showError(res.status + " " + res.statusText + "\n" + text);
    return;
  }

  if (!res.ok) {
    const detail = payload.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join("\n")
          : JSON.stringify(payload);
    showError(msg);
    return;
  }

  showResults(payload);
}

$("btn-score").addEventListener("click", score);

$("btn-load-sample").addEventListener("click", async () => {
  hideError();
  try {
    const res = await fetch("/api/sample");
    const text = await res.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      showError(res.status + " " + text);
      return;
    }
    if (!res.ok) {
      const d = payload.detail;
      showError(typeof d === "string" ? d : JSON.stringify(payload));
      return;
    }
    $("data").value = JSON.stringify(payload.data, null, 2);
    $("challenge").value = JSON.stringify(payload.challenge, null, 2);
    $("route").value = JSON.stringify(payload.route);
  } catch (e) {
    showError(e.message);
  }
});
