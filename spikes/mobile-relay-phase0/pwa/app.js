// Phase 0 spike client for issue #55. Talks to the bare relay in ../relay/
// over three endpoints: POST /pair, long-polling GET/POST on /mailbox/<id>,
// and POST /mailbox/<id>/decision. No encryption, no real pairing UX (see
// README) -- this exists to prove the pair -> wake -> decide loop is
// plausible over a phone browser, nothing more.

const STORAGE_KEY = "privacyfence-relay-spike-pairing";
const LONG_POLL_WAIT_SECONDS = 25;
const CLIENT_FETCH_TIMEOUT_MS = (LONG_POLL_WAIT_SECONDS + 10) * 1000;

const pairSection = document.getElementById("pair-section");
const waitingSection = document.getElementById("waiting-section");
const pendingSection = document.getElementById("pending-section");
const mailboxInfo = document.getElementById("mailbox-info");
const pendingCard = document.getElementById("pending-card");
const statusEl = document.getElementById("status");

let pollGeneration = 0; // bumped on unpair so an in-flight poll loop stops itself

function setStatus(text) {
  statusEl.textContent = text;
}

function loadPairing() {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw ? JSON.parse(raw) : null;
}

function savePairing(pairing) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(pairing));
}

function clearPairing() {
  localStorage.removeItem(STORAGE_KEY);
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function showSection(name) {
  pairSection.classList.toggle("hidden", name !== "pair");
  waitingSection.classList.toggle("hidden", name !== "waiting");
  pendingSection.classList.toggle("hidden", name !== "pending");
}

function renderPendingRequest(pairing, requestId, payload) {
  const piiWarning = payload && payload.pii_flagged
    ? '<p class="pii-flag">&#9888; This request contains flagged personal data.</p>'
    : "";
  const tool = (payload && payload.tool) || "(unknown tool)";
  const preview = (payload && payload.preview) || "(no preview provided)";
  const details = (payload && payload.details) || "";

  pendingCard.innerHTML = `
    ${piiWarning}
    <p><strong>${escapeHtml(tool)}</strong></p>
    <p>${escapeHtml(preview)}</p>
    ${details ? `<pre>${escapeHtml(details)}</pre>` : ""}
  `;
  pendingSection.dataset.requestId = requestId;
  showSection("pending");
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

async function pollForRequest(pairing, generation) {
  while (generation === pollGeneration) {
    setStatus("Waiting for a pending approval… (long-polling relay)");
    let response;
    try {
      response = await fetchWithTimeout(
        `${pairing.relayUrl}/mailbox/${pairing.mailboxId}?token=${encodeURIComponent(pairing.token)}&wait=${LONG_POLL_WAIT_SECONDS}`,
        { method: "GET" },
        CLIENT_FETCH_TIMEOUT_MS
      );
    } catch (err) {
      setStatus(`Relay unreachable (${err.message}) -- retrying…`);
      await sleep(3000);
      continue;
    }

    if (generation !== pollGeneration) return;

    if (response.status === 200) {
      const body = await response.json();
      renderPendingRequest(pairing, body.request_id, body.payload);
      return; // stop polling while a decision is pending -- resumed after Approve/Deny
    }
    if (response.status === 403) {
      setStatus("Pairing rejected by relay -- forgetting it. Pair again.");
      clearPairing();
      showSection("pair");
      return;
    }
    // 204: nothing pending yet -- loop straight back into another long poll.
  }
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function submitDecision(pairing, requestId, decision) {
  setStatus(`Sending "${decision}"…`);
  try {
    const response = await fetchWithTimeout(
      `${pairing.relayUrl}/mailbox/${pairing.mailboxId}/decision?token=${encodeURIComponent(pairing.token)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, decision }),
      },
      10000
    );
    if (response.status === 200) {
      setStatus(`Sent: ${decision}.`);
    } else if (response.status === 409) {
      setStatus("Too late -- this request was already resolved elsewhere.");
    } else if (response.status === 410) {
      setStatus("This request expired before you answered.");
    } else {
      setStatus(`Unexpected response (${response.status}).`);
    }
  } catch (err) {
    setStatus(`Failed to send decision: ${err.message}`);
  }
  showSection("waiting");
  pollGeneration += 1;
  pollForRequest(pairing, pollGeneration);
}

async function pair() {
  const relayUrl = document.getElementById("relay-url").value.replace(/\/$/, "");
  setStatus("Pairing…");
  try {
    const response = await fetchWithTimeout(`${relayUrl}/pair`, { method: "POST" }, 10000);
    if (!response.ok) {
      setStatus(`Pairing failed (${response.status}).`);
      return;
    }
    const body = await response.json();
    const pairing = { relayUrl, mailboxId: body.mailbox_id, token: body.token };
    savePairing(pairing);
    onPaired(pairing);
  } catch (err) {
    setStatus(`Pairing failed: ${err.message}`);
  }
}

function onPaired(pairing) {
  mailboxInfo.textContent = `Relay: ${pairing.relayUrl} | Mailbox: ${pairing.mailboxId}`;
  showSection("waiting");
  pollGeneration += 1;
  pollForRequest(pairing, pollGeneration);
}

function unpair() {
  pollGeneration += 1; // stops any in-flight poll loop
  clearPairing();
  showSection("pair");
  setStatus("Forgot pairing.");
}

document.getElementById("pair-button").addEventListener("click", pair);
document.getElementById("unpair-button").addEventListener("click", unpair);
document.getElementById("approve-button").addEventListener("click", () => {
  const pairing = loadPairing();
  submitDecision(pairing, pendingSection.dataset.requestId, "approved");
});
document.getElementById("deny-button").addEventListener("click", () => {
  const pairing = loadPairing();
  submitDecision(pairing, pendingSection.dataset.requestId, "denied");
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch((err) => {
    console.warn("service worker registration failed (non-fatal for this spike):", err);
  });
}

const existingPairing = loadPairing();
if (existingPairing) {
  onPaired(existingPairing);
} else {
  showSection("pair");
}
