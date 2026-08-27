// Phase 3 (issue #55): the real approval-inbox app. Pairs against a daemon
// via pairing.js's handshake, then long-polls its paired mailbox for
// approval requests -- encrypted+authenticated exactly the way
// MobileRelayApprovalUI (src/privacyfence/mobile_relay_approval_ui.py)
// sends and expects them. See README.md for what's real here vs. what's
// still a known gap (QR scanning, push-based wake, image/table parity).

"use strict";

const STORAGE_KEY = "privacyfence-mobile-approval-connection";
const LONG_POLL_SECONDS = 25;

const screens = {
  pair: document.getElementById("pair-screen"),
  waiting: document.getElementById("waiting-screen"),
  pending: document.getElementById("pending-screen"),
};
const statusEl = document.getElementById("status");
let pollGeneration = 0;

function setStatus(text) {
  statusEl.textContent = text;
}

function showScreen(name) {
  for (const [key, el] of Object.entries(screens)) el.classList.toggle("hidden", key !== name);
}

function loadConnection() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  const parsed = JSON.parse(raw);
  parsed.sharedKey = fromBase64(parsed.sharedKeyB64);
  return parsed;
}

function saveConnection(connection) {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      relayUrl: connection.relayUrl, mailboxId: connection.mailboxId,
      token: connection.token, sharedKeyB64: toBase64(connection.sharedKey),
      pwaReleasePublicKeyB64: connection.pwaReleasePublicKeyB64 || null,
    }),
  );
}

function clearConnection() {
  localStorage.removeItem(STORAGE_KEY);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

function renderPending(payload) {
  const card = document.getElementById("pending-card");
  const piiBanner = payload.pii_flagged
    ? `<p class="pii-banner">&#9888; This request contains flagged personal data${
        payload.pii_categories && payload.pii_categories.length ? `: ${escapeHtml(payload.pii_categories.join(", "))}` : ""
      }</p>`
    : "";

  if (payload.kind === "pii_confirmation") {
    card.innerHTML = `
      ${piiBanner}
      <p>PrivacyFence detected possible personal data in this content.</p>
      <p>Are you sure you want to proceed?</p>
    `;
    return;
  }
  if (payload.kind === "rule_confirmation") {
    card.innerHTML = `<p>Create this auto-accept rule?</p><pre>${escapeHtml(payload.description || "")}</pre>`;
    return;
  }

  const reason = payload.claude_reason
    ? `<p class="reason"><strong>Claude says:</strong> ${escapeHtml(payload.claude_reason)}</p>` : "";
  const previewRows = Object.entries(payload.preview || {})
    .map(([k, v]) => `<div class="preview-row"><span>${escapeHtml(k)}</span><span>${escapeHtml(v)}</span></div>`)
    .join("");

  card.innerHTML = `
    ${piiBanner}
    <p class="title">${escapeHtml(payload.title || "")}</p>
    ${payload.connector ? `<p class="connector">${escapeHtml(payload.connector)}</p>` : ""}
    ${previewRows}
    ${reason}
    ${payload.details_text ? `<pre class="details">${escapeHtml(payload.details_text)}</pre>` : ""}
  `;
}

async function pollForRequest(connection, generation) {
  while (generation === pollGeneration) {
    setStatus("Waiting for a pending approval…");
    let response;
    try {
      const query = new URLSearchParams({ token: connection.token, wait: LONG_POLL_SECONDS }).toString();
      response = await fetch(`${connection.relayUrl}/mailbox/${connection.mailboxId}?${query}`);
    } catch (err) {
      setStatus(`Relay unreachable (${err.message}) — retrying…`);
      await new Promise((resolve) => setTimeout(resolve, 3000));
      continue;
    }
    if (generation !== pollGeneration) return;

    if (response.status === 403) {
      setStatus("This pairing was rejected by the relay — forgetting it.");
      clearConnection();
      showScreen("pair");
      return;
    }
    if (response.status !== 200) continue; // 204 (nothing pending) or a transient error -- keep polling

    const body = await response.json();
    let decrypted;
    try {
      decrypted = await decryptPayload(connection.sharedKey, body.payload.ciphertext);
    } catch (err) {
      setStatus("Received a request that failed to decrypt — ignoring it (not trusted).");
      continue;
    }
    document.getElementById("pending-screen").dataset.requestId = body.request_id;
    renderPending(decrypted);
    showScreen("pending");
    return;
  }
}

async function submitDecision(connection, requestId, decision) {
  setStatus(`Sending "${decision}"…`);
  try {
    const auth = await computeAuthTag(connection.sharedKey, requestId, decision);
    const query = new URLSearchParams({ token: connection.token }).toString();
    const response = await fetch(`${connection.relayUrl}/mailbox/${connection.mailboxId}/decision?${query}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, decision, auth }),
    });
    if (response.status === 200) setStatus(`Sent: ${decision}.`);
    else if (response.status === 409) setStatus("Too late — already resolved elsewhere.");
    else if (response.status === 410) setStatus("This request expired before you answered.");
    else setStatus(`Unexpected response (${response.status}).`);
  } catch (err) {
    setStatus(`Failed to send decision: ${err.message}`);
  }
  showScreen("waiting");
  pollGeneration += 1;
  pollForRequest(connection, pollGeneration);
}

function onPaired(connection) {
  document.getElementById("connection-info").textContent =
    `Relay: ${connection.relayUrl} | Mailbox: ${connection.mailboxId}`;
  showScreen("waiting");
  pollGeneration += 1;
  pollForRequest(connection, pollGeneration);
}

/** Pins the org's PWA release public key (if the pairing payload carried
 * one) into the service worker's trust store, so it can verify signed
 * bundle updates -- see sw.js's own docstring. A phone paired before the
 * org had published a release key simply skips this; the next re-pairing
 * (e.g. after --rotate-mobile-relay-identity) picks it up once available. */
async function pinReleaseKeyWithServiceWorker(pwaReleasePublicKeyB64) {
  if (!pwaReleasePublicKeyB64) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    registration.active.postMessage({ type: "pin-release-key", publicKeyB64: pwaReleasePublicKeyB64 });
  } catch (err) {
    console.warn("Could not pin the PWA release key with the service worker:", err);
  }
}

async function handlePairSubmit() {
  const rawText = document.getElementById("pairing-payload").value;
  const deviceName = document.getElementById("device-name").value.trim() || "Unnamed phone";
  setStatus("Pairing…");
  try {
    const payload = parsePairingPayload(rawText);
    const connection = await completePairing(payload, deviceName);
    saveConnection(connection);
    await pinReleaseKeyWithServiceWorker(connection.pwaReleasePublicKeyB64);
    onPaired(connection);
  } catch (err) {
    setStatus(`Pairing failed: ${err.message}`);
  }
}

function handleForgetPairing() {
  pollGeneration += 1;
  clearConnection();
  showScreen("pair");
  setStatus("Forgot pairing.");
}

document.getElementById("pair-button").addEventListener("click", handlePairSubmit);
document.getElementById("forget-button").addEventListener("click", handleForgetPairing);
document.getElementById("approve-button").addEventListener("click", () => {
  const connection = loadConnection();
  submitDecision(connection, document.getElementById("pending-screen").dataset.requestId, "approved");
});
document.getElementById("deny-button").addEventListener("click", () => {
  const connection = loadConnection();
  submitDecision(connection, document.getElementById("pending-screen").dataset.requestId, "denied");
});

function renderBundleVerificationResult(result) {
  const banner = document.getElementById("bundle-warning");
  if (result.status === "failed") {
    banner.textContent = `⚠ PWA bundle verification failed: ${result.reason} Do not trust this app instance -- reinstall from a source you verify out of band.`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch((err) => {
    console.warn("Service worker registration failed (non-fatal):", err);
  });
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data && event.data.type === "bundle-verification-result") {
      renderBundleVerificationResult(event.data.result);
    }
  });
}

const existingConnection = loadConnection();
if (existingConnection) onPaired(existingConnection);
else showScreen("pair");
