"""HTML/CSS/vanilla-JS for the webview settings window (settings_window.py).

Visually transcribed from the design source (a Claude Design prototype
export, not shipped with this repo -- see the PR description for where it
lives) -- colors, spacing, radii, and the toggle/segmented-control visuals
below are copied from that file's inline styles, not approximated. What
*isn't* transcribed is that file's own rendering machinery (a small
declarative-component runtime, ``sc-if``/``sc-for``/``{{ }}`` tags): this
module is plain string templating plus a small amount of vanilla JS driving
the DOM directly, with no framework and no build step -- this is a fully
offline, file:// document (loaded via ``loadHTMLString_baseURL_``), so
nothing here may reference a CDN, a bundler-emitted asset, or the network.

No AppKit/WebKit import here either (see settings_controller.py's own
docstring for why) -- ``test_settings_window_html.py`` asserts on
``build_html()``'s output on any platform, and this module must stay
importable there.

State shape consumed by ``build_html()``/the JS ``render()`` function is
exactly ``SettingsController.snapshot()``'s return value, plus a per-
connector ``icon_data_uri`` field settings_window.py adds before handing the
dict here (icon embedding needs ``approval_window._icon_data_uri()``, which
*is* AppKit/WebKit-tainted -- see that module's docstring -- so it can't
happen in this file).

Bridge protocol (see settings_window.py's module docstring for the Python
side): the page's own ``post(action, payload)`` posts
``window.webkit.messageHandlers.pf.postMessage({action, ...payload})``;
Python answers by calling ``window.__pfRender(newState)`` after handling a
message or finishing a background op. Ephemeral, client-only UI state (which
nav section is active, which rules-connector/privacy-group is selected, the
rules search box's live text) lives in the JS-side ``ui`` object below and is
merged with the Python-pushed state on every render, using the same field
names the design's own ``Component.state`` used (``section``,
``rulesConnector``, ``privacyGroup``, ``rulesSearch``) -- never sent to
Python. Text inputs (rule_type/value, grant name/id, rules search) commit on
blur/Enter, not per keystroke, so a bridge round-trip mid-typing can't steal
focus/cursor position; toggles/segmented controls/buttons act immediately on
click since they're discrete, not free text.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------- #
# CSS -- values copied from the design source's inline styles.
# ---------------------------------------------------------------------------- #

_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; background: #f0f1f4; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', Helvetica, Arial, sans-serif;
  color: #1d1d1f;
  overflow: hidden;
}
::selection { background: rgba(0, 113, 227, .25); }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #c6c7cc; border-radius: 6px; }
::-webkit-scrollbar-track { background: transparent; }
input[type=text]:focus { outline: 2px solid #0071e3; outline-offset: 0; }

#app { display: flex; height: 100vh; overflow: hidden; }

/* ---- Left nav ---- */
.pf-nav {
  width: 190px; flex-shrink: 0; background: #e6e7eb; border-right: 1px solid #d3d4d9;
  padding: 14px 10px; display: flex; flex-direction: column; gap: 2px; height: 100%;
}
.pf-navitem {
  padding: 8px 12px; border-radius: 7px; font-size: 13px; cursor: pointer; font-weight: 400;
  color: #1d1d1f; background: transparent;
}
.pf-navitem.active { font-weight: 600; background: #0071e3; color: #fff; }
.pf-nav-spacer { flex: 1; }
.pf-nav-version { padding: 8px 10px; font-size: 11px; color: #8a8a8e; }

/* ---- Content shell ---- */
.pf-content { flex: 1; overflow: hidden; display: flex; background: #fff; min-width: 0; }
.pf-page { flex: 1; overflow-y: auto; padding: 36px 44px; }
.pf-page-title { font-size: 22px; font-weight: 700; color: #1d1d1f; margin: 0 0 22px; }
.pf-page-subtitle { font-size: 12px; color: #6e6e73; margin-bottom: 22px; max-width: 600px; line-height: 1.5; }

/* ---- Error banner ---- */
.pf-error-banner {
  background: #fef2f1; border: 1px solid #f0c2bd; color: #b3261e; border-radius: 8px;
  padding: 10px 14px; font-size: 12.5px; margin: 16px 44px 0; display: flex;
  align-items: center; justify-content: space-between; gap: 12px;
}
.pf-error-dismiss { cursor: pointer; color: #b3261e; font-weight: 600; flex-shrink: 0; }

/* ---- Cards / rows shared across pages ---- */
.pf-card {
  background: #f7f7f8; border: 1px solid #e5e5ea; border-radius: 10px; padding: 16px 20px;
  margin-bottom: 16px; max-width: 620px;
}
.pf-card-row { display: flex; align-items: center; justify-content: space-between; }
.pf-card-title { font-size: 14px; font-weight: 600; color: #1d1d1f; }
.pf-card-desc { font-size: 12px; color: #6e6e73; margin-top: 3px; max-width: 440px; line-height: 1.4; }
.pf-divider { height: 1px; background: #e5e5ea; margin: 14px 0; }
.pf-subrow { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; }
.pf-subrow-label { font-size: 13px; color: #1d1d1f; }

/* ---- Toggle switch ---- */
.pf-toggle {
  width: 40px; height: 24px; border-radius: 12px; cursor: pointer; transition: background .15s;
  background: #d7d8dd; position: relative; flex-shrink: 0;
}
.pf-toggle.on { background: #0071e3; }
.pf-toggle.disabled { cursor: default; opacity: .5; }
.pf-knob {
  width: 20px; height: 20px; border-radius: 50%; background: #fff; position: relative;
  top: 2px; left: 2px; transition: left .15s; box-shadow: 0 1px 2px rgba(0,0,0,.25);
}
.pf-toggle.on .pf-knob { left: 20px; }

/* ---- Buttons ---- */
.pf-btn-primary {
  background: #0071e3; color: #fff; border: none; border-radius: 7px; padding: 7px 14px;
  font-size: 13px; font-weight: 500; cursor: pointer;
}
.pf-btn-secondary {
  background: #eceef1; color: #1d1d1f; border: none; border-radius: 7px; padding: 8px 16px;
  font-size: 13px; font-weight: 500; cursor: pointer;
}
.pf-btn-danger {
  background: #fff; color: #d92d20; border: 1px solid #f0c2bd; border-radius: 7px;
  padding: 8px 16px; font-size: 13px; font-weight: 500; cursor: pointer;
}
.pf-link { font-size: 12.5px; color: #0071e3; cursor: pointer; }
.pf-link-danger { font-size: 12px; color: #d92d20; cursor: pointer; white-space: nowrap; }

/* ---- Segmented controls ---- */
.pf-seg-group { display: flex; background: #eceef1; border-radius: 7px; padding: 2px; flex-shrink: 0; }
.pf-seg-btn { padding: 5px 12px; font-size: 12px; font-weight: 500; border-radius: 5px; cursor: pointer; color: #6e6e73; }
.pf-seg-btn.plain-active { background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.15); color: #1d1d1f; }
.pf-seg-btn.policy-allow { background: #0071e3; color: #fff; }
.pf-seg-btn.policy-redact { background: #b76e00; color: #fff; }
.pf-seg-btn.policy-block { background: #d92d20; color: #fff; }

/* ---- Text inputs ---- */
.pf-input {
  border: 1px solid #d3d4d9; border-radius: 6px; padding: 5px 8px; font-size: 12.5px; background: #fff;
}
.pf-input-mono { font-family: ui-monospace, monospace; }

/* ---- Connectors page ---- */
.pf-connector-row {
  display: flex; align-items: center; gap: 14px; padding: 12px 4px; border-bottom: 1px solid #ececef;
  max-width: 760px;
}
.pf-connector-icon {
  width: 34px; height: 34px; border-radius: 9px; background: #e9eaee; display: flex;
  align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden;
}
.pf-connector-icon img { width: 22px; height: 22px; object-fit: contain; }
.pf-connector-label { width: 150px; font-size: 13.5px; color: #1d1d1f; font-weight: 500; flex-shrink: 0; }
.pf-pill { font-size: 11px; padding: 3px 9px; border-radius: 10px; white-space: nowrap; }
.pf-pill-connected { background: rgba(0,113,227,.1); color: #0071e3; }
.pf-pill-neutral { background: #f0f0f2; color: #6e6e73; }
.pf-pill-warn { background: #fff3cd; color: #8a5a00; }
.pf-pill-missing { background: #fef2f1; color: #d92d20; }
.pf-spacer { flex: 1; }
.pf-auth-link { font-size: 12.5px; color: #0071e3; cursor: pointer; white-space: nowrap; }
.pf-auth-link.disabled { color: #b3b3b8; cursor: default; pointer-events: none; }

/* ---- Rules / Privacy shared 2-pane layout ---- */
.pf-subnav {
  width: 170px; flex-shrink: 0; background: #f7f7f8; border-right: 1px solid #e5e5ea;
  padding: 12px 10px; display: flex; flex-direction: column; overflow-y: auto;
}
.pf-subnav-search { margin-bottom: 10px; width: 100%; }
.pf-subnav-item {
  padding: 7px 10px; border-radius: 7px; font-size: 13px; cursor: pointer; display: flex;
  justify-content: space-between; margin-bottom: 1px; color: #1d1d1f; font-weight: 400;
}
.pf-subnav-item.active { background: #0071e3; color: #fff; font-weight: 600; }
.pf-subnav-count { font-size: 11px; opacity: .75; }
.pf-detail-page { flex: 1; overflow-y: auto; padding: 28px 36px; }
.pf-detail-title { font-size: 18px; font-weight: 700; color: #1d1d1f; margin-bottom: 2px; }
.pf-detail-subtitle { font-size: 12px; color: #6e6e73; margin-bottom: 20px; max-width: 520px; line-height: 1.5; }

/* ---- Grants ---- */
.pf-group-title { font-size: 13px; font-weight: 600; color: #6e6e73; margin-bottom: 8px; }
.pf-grant-section { margin-bottom: 22px; }
.pf-grant-row { background: #f7f7f8; border: 1px solid #e5e5ea; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }
.pf-grant-row-fields { display: flex; align-items: center; gap: 10px; }
.pf-grant-row-fields .pf-input { flex: 1; }
.pf-caps-row { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.pf-cap-chip { padding: 4px 10px; border-radius: 5px; font-size: 11px; cursor: pointer; font-weight: 500; background: #eceef1; color: #6e6e73; }
.pf-cap-chip.on { background: #0071e3; color: #fff; }

/* ---- Rules ---- */
.pf-rule-section { margin-bottom: 20px; }
.pf-rule-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pf-rule-row .pf-input-type { width: 190px; flex-shrink: 0; }
.pf-rule-row .pf-input-value { flex: 1; }
.pf-rules-empty { font-size: 13px; color: #8a8a8e; }

/* ---- Privacy ---- */
.pf-policy-row {
  display: flex; align-items: center; justify-content: space-between; padding: 12px 14px;
  background: #f7f7f8; border: 1px solid #e5e5ea; border-radius: 8px; margin-bottom: 14px; max-width: 560px;
}
.pf-policy-row-label { font-size: 13px; font-weight: 600; color: #1d1d1f; }
.pf-policy-row-label .pf-muted { font-weight: 400; color: #8a8a8e; }
.pf-category-row {
  display: flex; align-items: center; justify-content: space-between; padding: 11px 14px;
  border-bottom: 1px solid #ececef; max-width: 560px;
}
.pf-category-label { font-size: 13.5px; color: #1d1d1f; font-weight: 500; }
.pf-category-key { font-size: 11.5px; color: #8a8a8e; font-family: ui-monospace, monospace; margin-top: 2px; }

/* ---- Audit ---- */
.pf-export-row { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
.pf-export-hint { font-size: 12px; color: #6e6e73; }
.pf-audit-card { max-width: 640px; margin-bottom: 22px; }
.pf-audit-card-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.pf-audit-card-row:last-child { margin-bottom: 0; }
.pf-audit-card-title { font-size: 13.5px; font-weight: 600; color: #1d1d1f; }
.pf-audit-logfile { font-size: 12px; color: #6e6e73; font-family: ui-monospace, monospace; }
.pf-audit-list { max-width: 640px; border: 1px solid #e5e5ea; border-radius: 10px; overflow: hidden; }
.pf-audit-row {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: #fff;
  border-bottom: 1px solid #ececef;
}
.pf-audit-row:last-child { border-bottom: none; }
.pf-audit-connector { width: 80px; font-size: 12px; color: #6e6e73; flex-shrink: 0; }
.pf-audit-tool { flex: 1; font-size: 12.5px; color: #1d1d1f; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pf-audit-badge { font-size: 10.5px; font-weight: 600; padding: 3px 8px; border-radius: 9px; flex-shrink: 0; }
.pf-audit-badge.denied { background: #fef2f1; color: #d92d20; }
.pf-audit-badge.auto_accepted { background: rgba(0,113,227,.1); color: #0071e3; }
.pf-audit-badge.other { background: #eceef1; color: #4a4a4e; }
.pf-audit-time { width: 70px; text-align: right; font-size: 11.5px; color: #8a8a8e; flex-shrink: 0; }

/* ---- About ---- */
.pf-about-page {
  flex: 1; overflow-y: auto; padding: 56px 44px; display: flex; flex-direction: column;
  align-items: center; text-align: center;
}
.pf-about-icon {
  width: 76px; height: 76px; border-radius: 18px; background: #0071e3; color: #fff; font-size: 26px;
  font-weight: 700; display: flex; align-items: center; justify-content: center; margin-bottom: 18px;
}
.pf-about-name { font-size: 20px; font-weight: 700; color: #1d1d1f; }
.pf-about-version { font-size: 13px; color: #6e6e73; margin-top: 4px; }
.pf-about-desc { font-size: 13px; color: #4a4a4e; margin-top: 18px; max-width: 420px; line-height: 1.5; }
.pf-about-repo { margin-top: 20px; font-size: 13px; color: #0071e3; cursor: pointer; }
.pf-about-license { font-size: 12px; color: #8a8a8e; margin-top: 6px; }
.pf-about-buttons { display: flex; gap: 12px; margin-top: 28px; }

/* ---- Telegram sign-in modal ---- */
/* Not part of the design mockup (which has no multi-step-form concept
   anywhere) -- kept visually consistent with the rest of the app (same
   fonts/colors/button styles already defined above) rather than a new
   look of its own. */
.pf-modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex;
  align-items: center; justify-content: center; z-index: 100;
}
.pf-modal {
  background: #fff; border-radius: 12px; padding: 24px 28px; width: 360px;
  box-shadow: 0 20px 60px rgba(0,0,0,.35);
}
.pf-modal-title { font-size: 15px; font-weight: 600; color: #1d1d1f; margin-bottom: 4px; }
.pf-modal-desc { font-size: 12px; color: #6e6e73; margin-bottom: 14px; line-height: 1.4; }
.pf-modal-error { font-size: 12px; color: #d92d20; margin-bottom: 10px; }
.pf-modal-input { width: 100%; margin-bottom: 16px; }
.pf-modal-buttons { display: flex; justify-content: flex-end; gap: 10px; }
"""

# ---------------------------------------------------------------------------- #
# JS -- render(state) rebuilds #app's innerHTML from the merged python+ui
# state on every call; post() is the JS->Python half of the bridge.
# ---------------------------------------------------------------------------- #

_JS = r"""
(function () {
  var ui = {
    section: 'general', rulesConnector: null, privacyGroup: null, rulesSearch: '',
    telegramModalOpen: false,
    // Tracks whether we've actually observed a non-null telegram_auth.step
    // from Python yet -- a fresh, never-submitted modal also has step ===
    // null, and without this the auto-close-on-success check in render()
    // couldn't tell "flow just succeeded" apart from "flow never started".
    telegramAuthWasActive: false,
  };
  var pyState = null;

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function post(action, payload) {
    var msg = Object.assign({ action: action }, payload || {});
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.pf) {
      window.webkit.messageHandlers.pf.postMessage(msg);
    }
  }

  function dataAttr(action, payload) {
    return 'data-action="' + esc(action) + '" data-payload=\'' + JSON.stringify(payload || {}).replace(/'/g, '&#39;') + '\'';
  }

  function toggleHtml(on, action, payload, disabled, ariaLabel) {
    // role="switch"/aria-checked (not role="button") -- this is a genuine
    // binary on/off control, and a QA/AT script needs a real checked state
    // to read back, not just a clickable target. See the PR report for
    // exactly which System-Events AX path reads this in practice.
    var cls = 'pf-toggle' + (on ? ' on' : '') + (disabled ? ' disabled' : '');
    var attrs = disabled ? '' : dataAttr(action, payload);
    return '<div class="' + cls + '" role="switch" aria-checked="' + (on ? 'true' : 'false') + '" ' +
      (disabled ? 'aria-disabled="true"' : 'tabindex="0"') +
      (ariaLabel ? ' aria-label="' + esc(ariaLabel) + '"' : '') + ' ' + attrs +
      '><div class="pf-knob"></div></div>';
  }

  function segGroupHtml(items, groupLabel) {
    // items: [{label, active, action, payload, colorClass}]
    // role="radiogroup"/"radio" -- a segmented control is a mutually
    // exclusive choice among named options, the same semantics as a radio
    // group, not a set of independent buttons.
    var html = '<div class="pf-seg-group" role="radiogroup"' + (groupLabel ? ' aria-label="' + esc(groupLabel) + '"' : '') + '>';
    items.forEach(function (it) {
      var cls = 'pf-seg-btn' + (it.active ? ' ' + (it.colorClass || 'plain-active') : '');
      var optionLabel = groupLabel ? groupLabel + ': ' + it.label : it.label;
      html += '<div class="' + cls + '" role="radio" aria-checked="' + (it.active ? 'true' : 'false') +
        '" tabindex="0" aria-label="' + esc(optionLabel) + '" ' + dataAttr(it.action, it.payload) + '>' + esc(it.label) + '</div>';
    });
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Nav
  // -------------------------------------------------------------------- //

  var NAV_ITEMS = [
    ['general', 'General'], ['connectors', 'Connectors'], ['rules', 'Auto-accept Rules'],
    ['privacy', 'Privacy Filter'], ['audit', 'Audit Log'], ['about', 'About'],
  ];

  function renderNav(state) {
    var html = '<div class="pf-nav" role="tablist" aria-label="Settings sections">';
    NAV_ITEMS.forEach(function (item) {
      var key = item[0], label = item[1];
      var active = ui.section === key;
      html += '<div class="pf-navitem' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(label) + '" data-nav="' + key + '">' + esc(label) + '</div>';
    });
    html += '<div class="pf-nav-spacer"></div>';
    html += '<div class="pf-nav-version">PrivacyFence ' + esc(state.about.version) + '</div>';
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // General
  // -------------------------------------------------------------------- //

  function renderGeneral(state) {
    var g = state.general;
    var html = '<div class="pf-page">';
    html += '<div class="pf-page-title">General</div>';

    html += '<div class="pf-card">';
    html += '<div class="pf-card-row"><div><div class="pf-card-title">PII Detection Gate</div>';
    html += '<div class="pf-card-desc">Scans review-popup content for likely personal data (IBANs, national IDs, financial figures) before you approve it. A match requires a second confirmation.</div></div>';
    html += toggleHtml(g.pii_enabled, 'toggle_pii_detection', {}, false, 'PII Detection Gate');
    html += '</div>';
    html += '<div class="pf-divider"></div>';
    html += '<div class="pf-subrow" style="opacity:' + (g.pii_enabled ? 1 : .4) + '"><div class="pf-subrow-label">Detect IP addresses</div>';
    html += toggleHtml(g.pii_ip, 'toggle_pii_category', { category_key: 'detect_ip_addresses' }, !g.pii_enabled, 'Detect IP addresses');
    html += '</div>';
    html += '<div class="pf-subrow" style="opacity:' + (g.pii_enabled ? 1 : .4) + '"><div class="pf-subrow-label">Detect financial figures</div>';
    html += toggleHtml(g.pii_financial, 'toggle_pii_category', { category_key: 'detect_financial_figures' }, !g.pii_enabled, 'Detect financial figures');
    html += '</div></div>';

    html += '<div class="pf-card"><div class="pf-card-row"><div><div class="pf-card-title">Check for Updates</div>';
    html += '<div class="pf-card-desc">Once-a-day check against GitHub Releases. Never installs anything automatically.</div></div>';
    html += toggleHtml(g.update_check_enabled, 'toggle_update_check', {}, false, 'Check for Updates');
    html += '</div><div class="pf-divider"></div>';
    html += '<div class="pf-subrow" style="opacity:' + (g.update_check_enabled ? 1 : .4) + '"><div class="pf-subrow-label">Receive beta releases</div>';
    html += toggleHtml(g.update_check_beta, 'toggle_update_check_beta', {}, !g.update_check_enabled, 'Receive beta releases');
    html += '</div></div>';

    html += '<div class="pf-card"><div class="pf-card-title">Organization Configuration</div>';
    html += '<div class="pf-card-desc" style="margin-bottom:12px;">OAuth app credentials and unattended-session policy, provided by your IT administrator.</div>';
    html += '<div style="display:flex;align-items:center;gap:14px;">';
    html += '<div class="pf-btn-primary" role="button" tabindex="0" aria-label="' + esc(g.org_button_label) + '" ' +
      dataAttr('install_org_config', {}) + '>' + esc(g.org_button_label) + '</div>';
    if (g.org_installed && g.org_installed_date) {
      html += '<div class="pf-export-hint">Installed ' + esc(g.org_installed_date) + '</div>';
    } else if (!g.org_installed) {
      html += '<div class="pf-export-hint">Not installed</div>';
    }
    html += '</div></div>';

    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Connectors
  // -------------------------------------------------------------------- //

  function connectorStatus(c) {
    if (c.busy) return { text: 'Connecting…', cls: 'pf-pill-warn' };
    if (c.authed) return { text: 'Connected', cls: 'pf-pill-connected' };
    if (!c.enabled) return { text: 'Disabled', cls: 'pf-pill-neutral' };
    if (!c.has_org) {
      return { text: c.key === 'telegram' ? 'App credentials missing' : 'Organization config missing', cls: 'pf-pill-missing' };
    }
    return { text: 'Not connected', cls: 'pf-pill-neutral' };
  }

  function renderConnectors(state) {
    var html = '<div class="pf-page">';
    html += '<div class="pf-page-title">Connectors</div>';
    html += '<div class="pf-page-subtitle">Authenticate a connector to let Claude access it, subject to approval and policy.</div>';
    state.connectors.forEach(function (c) {
      var status = connectorStatus(c);
      html += '<div class="pf-connector-row">';
      html += '<div class="pf-connector-icon">' + (c.icon_data_uri ? '<img src="' + esc(c.icon_data_uri) + '" alt="' + esc(c.label) + '"/>' : '') + '</div>';
      html += '<div class="pf-connector-label">' + esc(c.label) + '</div>';
      html += '<div class="pf-pill ' + status.cls + '">' + esc(status.text) + '</div>';
      html += '<div class="pf-spacer"></div>';
      var authDisabled = c.busy;
      if (c.key === 'telegram') {
        // Telegram's phone/code/2FA flow needs its own multi-step modal
        // (see renderTelegramModal below) instead of the generic single-
        // click OAuth flow every other connector uses -- intercepted here
        // client-side (data-telegram-auth, not data-action) so opening the
        // modal at the phone-entry step needs no round trip to Python;
        // the first real bridge call is telegram_start_auth() once a
        // phone number is actually submitted.
        html += '<div class="pf-auth-link' + (authDisabled ? ' disabled' : '') +
          '" role="button" tabindex="0" aria-label="' + esc(c.auth_label) + ' Telegram"' +
          (authDisabled ? '' : ' data-telegram-auth="1"') + '>' + esc(c.auth_label) + '</div>';
      } else {
        html += '<div class="pf-auth-link' + (authDisabled ? ' disabled' : '') +
          '" role="button" tabindex="0" aria-label="' + esc(c.auth_label) + ' ' + esc(c.label) + '" ' +
          (authDisabled ? '' : dataAttr('authenticate_connector', { connector: c.key })) + '>' + esc(c.auth_label) + '</div>';
      }
      html += toggleHtml(c.enabled, 'toggle_connector', { connector: c.key }, false, c.label + ' enabled');
      html += '</div>';
    });
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Rules
  // -------------------------------------------------------------------- //

  function renderSuggestionPriority(sp, connectorKey) {
    // "Always-allow Suggestion Order" -- which rule Always allow proposes
    // first when a read could match more than one (e.g. Drive's i_am_owner
    // vs. approved_folder), user-reorderable, and excludable by moving it
    // out of the included list entirely. Only rendered for the connectors
    // SUGGESTION_FAMILY_BY_CONNECTOR covers (see settings_controller.py) --
    // sp is null for every other connector. Reuses the same pf-link/
    // pf-link-danger row language as the rule/grant sections above rather
    // than introducing new styles.
    if (!sp) return '';
    var html = '<div class="pf-rule-section"><div class="pf-group-title">Always-allow Suggestion Order</div>';
    sp.included.forEach(function (ruleName, i) {
      html += '<div class="pf-rule-row"><div class="pf-input-value pf-input-mono" style="border:none;background:transparent;padding:5px 0;">' + esc(ruleName) + '</div>';
      if (i > 0) {
        html += '<div class="pf-link" role="button" tabindex="0" aria-label="Move ' + esc(ruleName) + ' up" ' +
          dataAttr('move_suggestion_priority', { connector: connectorKey, direction: -1, rule_name: ruleName }) + '>↑ Move up</div>';
      }
      if (i < sp.included.length - 1) {
        html += '<div class="pf-link" role="button" tabindex="0" aria-label="Move ' + esc(ruleName) + ' down" ' +
          dataAttr('move_suggestion_priority', { connector: connectorKey, direction: 1, rule_name: ruleName }) + '>↓ Move down</div>';
      }
      html += '<div class="pf-link-danger" role="button" tabindex="0" aria-label="Never suggest ' + esc(ruleName) + '" ' +
        dataAttr('exclude_suggestion_rule', { connector: connectorKey, rule_name: ruleName }) + '>✕ Never suggest</div>';
      html += '</div>';
    });
    sp.excluded.forEach(function (ruleName) {
      html += '<div class="pf-rule-row"><div class="pf-input-value pf-input-mono" style="border:none;background:transparent;padding:5px 0;color:#b3b3b8;">' +
        esc(ruleName) + ' (excluded)</div>';
      html += '<div class="pf-link" role="button" tabindex="0" aria-label="Re-include ' + esc(ruleName) + '" ' +
        dataAttr('include_suggestion_rule', { connector: connectorKey, rule_name: ruleName }) + '>+ Re-include</div>';
      html += '</div>';
    });
    html += '</div>';
    return html;
  }

  function renderRules(state) {
    var rules = state.rules;
    if (!ui.rulesConnector && rules.connectors.length) ui.rulesConnector = rules.connectors[0].key;
    var search = (ui.rulesSearch || '').trim().toLowerCase();

    var html = '<div class="pf-subnav">';
    html += '<input type="text" class="pf-input pf-subnav-search" placeholder="Search rules…" aria-label="Search rules" value="' +
      esc(ui.rulesSearch) + '" data-rules-search="1"/>';
    html += '<div role="tablist" aria-label="Connector">';
    rules.connectors.forEach(function (rc) {
      var active = ui.rulesConnector === rc.key;
      html += '<div class="pf-subnav-item' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(rc.label) +
        '" data-rules-nav="' + esc(rc.key) + '"><span>' + esc(rc.label) + '</span>';
      if (rc.count) html += '<span class="pf-subnav-count">' + rc.count + '</span>';
      html += '</div>';
    });
    html += '</div></div>';

    var curKey = ui.rulesConnector;
    var curLabel = '';
    rules.connectors.forEach(function (rc) { if (rc.key === curKey) curLabel = rc.label; });
    var grantSections = (rules.grants_by_connector[curKey] || []);
    var ruleSections = (rules.sections_by_connector[curKey] || []);

    html += '<div class="pf-detail-page">';
    html += '<div class="pf-detail-title">' + esc(curLabel) + '</div>';
    html += '<div class="pf-detail-subtitle">Auto-accept rules and trusted resources for ' + esc(curLabel) + '.</div>';

    var driveSummary = (rules.drive_grant_summary_by_connector || {})[curKey];
    if (driveSummary && !search) {
      html += '<div class="pf-grant-section"><div class="pf-group-title">' + esc(driveSummary.title) + '</div>';
      driveSummary.rows.forEach(function (row) {
        html += '<div class="pf-rule-row"><div class="pf-input-value" style="border:none;background:transparent;padding:5px 0;">' +
          '<strong>' + esc(row.label) + ':</strong> ' + esc(row.value) + '</div></div>';
      });
      html += '<div class="pf-link" role="button" tabindex="0" aria-label="' + esc(driveSummary.link_label) + '" ' +
        'data-rules-nav="drive">' + esc(driveSummary.link_label) + '</div>';
      html += '</div>';
    }

    grantSections.forEach(function (gs) {
      var matchingRows = gs.rows.map(function (row, idx) { return { row: row, idx: idx }; }).filter(function (r) {
        if (!search) return true;
        return (r.row.name + ' ' + r.row.id).toLowerCase().indexOf(search) !== -1 || gs.title.toLowerCase().indexOf(search) !== -1;
      });
      if (search && matchingRows.length === 0 && gs.title.toLowerCase().indexOf(search) === -1) return;
      html += '<div class="pf-grant-section"><div class="pf-group-title">' + esc(gs.title) + '</div>';
      matchingRows.forEach(function (r) {
        var row = r.row, idx = r.idx;
        html += '<div class="pf-grant-row"><div class="pf-grant-row-fields">';
        html += '<input type="text" class="pf-input" placeholder="Name" aria-label="' + esc(gs.title) + ' name" value="' + esc(row.name) + '" ' +
          'data-grant-field="name" data-connector="' + esc(curKey) + '" data-config-key="' + esc(gs.config_key) + '" data-idx="' + idx + '"/>';
        html += '<input type="text" class="pf-input pf-input-mono" placeholder="Resource ID" aria-label="' + esc(gs.title) + ' resource ID" value="' + esc(row.id) + '" ' +
          'data-grant-field="id" data-connector="' + esc(curKey) + '" data-config-key="' + esc(gs.config_key) + '" data-idx="' + idx + '"/>';
        html += '<div class="pf-link-danger" role="button" tabindex="0" aria-label="Remove ' + esc(row.name || row.id || gs.title) + '" ' +
          dataAttr('remove_grant_row', { connector: curKey, config_key: gs.config_key, idx: idx }) + '>✕ Remove</div>';
        html += '</div><div class="pf-caps-row">';
        gs.cap_keys.forEach(function (capKey) {
          var on = !!row.caps[capKey];
          var capLabel = gs.cap_labels[capKey] || capKey;
          html += '<div class="pf-cap-chip' + (on ? ' on' : '') + '" role="checkbox" aria-checked="' + (on ? 'true' : 'false') +
            '" tabindex="0" aria-label="' + esc(capLabel) + '" ' +
            dataAttr('toggle_grant_capability', { connector: curKey, config_key: gs.config_key, idx: idx, cap: capKey }) + '>' +
            esc(capLabel) + '</div>';
        });
        html += '</div></div>';
      });
      html += '<div class="pf-link" role="button" tabindex="0" aria-label="' + esc(gs.add_label) + '" ' +
        dataAttr('add_grant_row', { connector: curKey, config_key: gs.config_key }) + '>+ ' + esc(gs.add_label) + '</div>';
      html += '</div>';
    });

    var totalRows = 0;
    ruleSections.forEach(function (sec) {
      var matches = !search || sec.title.toLowerCase().indexOf(search) !== -1 ||
        sec.rows.some(function (r) { return (r.rule_type + ' ' + r.value).toLowerCase().indexOf(search) !== -1; });
      if (!matches) return;
      totalRows += sec.rows.length;
      html += '<div class="pf-rule-section"><div class="pf-group-title">' + esc(sec.title) + '</div>';
      sec.rows.forEach(function (row, idx) {
        html += '<div class="pf-rule-row">';
        html += '<input type="text" class="pf-input pf-input-type pf-input-mono" placeholder="rule_type" aria-label="' +
          esc(sec.title) + ' rule type, row ' + (idx + 1) + '" value="' + esc(row.rule_type) + '" ' +
          'data-rule-field="rule_type" data-op-key="' + esc(sec.op_key) + '" data-idx="' + idx + '"/>';
        html += '<input type="text" class="pf-input pf-input-value" placeholder="value" aria-label="' +
          esc(sec.title) + ' value, row ' + (idx + 1) + '" value="' + esc(row.value) + '" ' +
          'data-rule-field="value" data-op-key="' + esc(sec.op_key) + '" data-idx="' + idx + '"/>';
        html += '<div class="pf-link-danger" role="button" tabindex="0" aria-label="Remove ' + esc(sec.title) + ' row ' + (idx + 1) + '" ' +
          dataAttr('remove_rule_row', { op_key: sec.op_key, idx: idx }) + '>✕ Remove</div>';
        html += '</div>';
      });
      html += '<div class="pf-link" role="button" tabindex="0" aria-label="Add rule to ' + esc(sec.title) + '" ' +
        dataAttr('add_rule_row', { op_key: sec.op_key }) + '>+ Add rule…</div>';
      html += '</div>';
    });

    var suggestionPriority = (rules.suggestion_priority_by_connector || {})[curKey] || null;
    if (!search) html += renderSuggestionPriority(suggestionPriority, curKey);

    var anyGrantRows = grantSections.some(function (gs) { return gs.rows.length > 0; });
    if (search && totalRows === 0 && !anyGrantRows) {
      html += '<div class="pf-rules-empty">' + (search ? 'No matches.' : 'Nothing here.') + '</div>';
    } else if (!search && ruleSections.length === 0 && grantSections.length === 0 && !suggestionPriority && !driveSummary) {
      html += '<div class="pf-rules-empty">All operations always auto-approved — no rules needed.</div>';
    }

    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Privacy
  // -------------------------------------------------------------------- //

  var POLICY_COLOR_CLASS = { allow: 'policy-allow', redact: 'policy-redact', block: 'policy-block' };

  function policySegHtml(current, onAction, basePayload, groupLabel) {
    return segGroupHtml(['allow', 'redact', 'block'].map(function (p) {
      var payload = Object.assign({}, basePayload, { policy: p });
      return { label: p.charAt(0).toUpperCase() + p.slice(1), active: current === p, action: onAction, payload: payload, colorClass: POLICY_COLOR_CLASS[p] };
    }), groupLabel);
  }

  function renderPrivacy(state) {
    var privacy = state.privacy;
    if (!ui.privacyGroup && privacy.groups.length) ui.privacyGroup = privacy.groups[0].key;

    var html = '<div class="pf-subnav" role="tablist" aria-label="Privacy Filter group">';
    privacy.groups.forEach(function (pg) {
      var active = ui.privacyGroup === pg.key;
      html += '<div class="pf-subnav-item' + (active ? ' active' : '') + '" role="tab" aria-selected="' +
        (active ? 'true' : 'false') + '" tabindex="0" aria-label="' + esc(pg.label) +
        '" data-privacy-nav="' + esc(pg.key) + '"><span>' + esc(pg.label) + '</span></div>';
    });
    html += '</div>';

    html += '<div class="pf-detail-page">';
    if (ui.privacyGroup === 'calendar') {
      html += '<div class="pf-detail-title">Calendar</div>';
      html += '<div class="pf-detail-subtitle">Calendar has no category schema — this is its one privacy-relevant setting.</div>';
      html += '<div class="pf-card" style="max-width:560px;"><div class="pf-card-row"><div>';
      html += '<div class="pf-card-title" style="font-size:13.5px;">Show full event details in free/busy</div>';
      html += '<div class="pf-card-desc">When off, calendar_get_free_busy always returns busy/free blocks only, never titles or status, regardless of access.</div>';
      html += '</div>' + toggleHtml(privacy.calendar_free_busy, 'toggle_calendar_free_busy', {}, false, 'Show full event details in free/busy') + '</div></div>';
    } else {
      var group = ui.privacyGroup;
      var label = '';
      privacy.groups.forEach(function (pg) { if (pg.key === group) label = pg.label; });
      var defaultPolicy = privacy.default_policy[group];
      var categories = privacy.categories[group] || [];

      html += '<div class="pf-detail-title">' + esc(label) + '</div>';
      html += '<div class="pf-detail-subtitle">Applied before this data reaches the review popup, Claude, or the audit log — a floor under human review, not a substitute for it.</div>';

      html += '<div class="pf-policy-row"><div class="pf-policy-row-label">Default policy <span class="pf-muted">(unlisted categories)</span></div>';
      html += policySegHtml(defaultPolicy, 'set_default_policy', { group: group }, 'Default policy');
      html += '</div>';

      categories.forEach(function (cat) {
        html += '<div class="pf-category-row"><div><div class="pf-category-label">' + esc(cat.label) + '</div>';
        html += '<div class="pf-category-key">' + esc(cat.key) + '</div></div>';
        html += policySegHtml(cat.policy, 'set_category_policy', { group: group, category: cat.key }, cat.label + ' policy');
        html += '</div>';
      });
    }
    html += '</div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Audit
  // -------------------------------------------------------------------- //

  var LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

  function renderAudit(state) {
    var audit = state.audit;
    var html = '<div class="pf-page">';
    html += '<div class="pf-page-title">Audit Log</div>';
    html += '<div class="pf-page-subtitle">Every decision — accepted, denied, or auto-accepted — is recorded locally as JSON lines, then exported weekly to a formatted Excel workbook.</div>';

    html += '<div class="pf-export-row"><div class="pf-btn-primary" role="button" tabindex="0" aria-label="Export Audit Log" ' +
      dataAttr('export_audit_log', {}) + '>Export Audit Log…</div>';
    html += '<div class="pf-export-hint">' + esc(audit.export_hint) + '</div></div>';

    html += '<div class="pf-card pf-audit-card">';
    html += '<div class="pf-audit-card-row"><div class="pf-audit-card-title">Log level</div>';
    html += segGroupHtml(LOG_LEVELS.map(function (lvl) {
      return { label: lvl, active: audit.log_level === lvl, action: 'set_log_level', payload: { level: lvl } };
    }), 'Log level');
    html += '</div>';
    html += '<div class="pf-audit-card-row"><div class="pf-audit-card-title">Log file</div>';
    html += '<div class="pf-audit-logfile">' + esc(audit.log_file) + '</div></div>';
    html += '</div>';

    html += '<div class="pf-group-title">Recent decisions</div>';
    html += '<div class="pf-audit-list">';
    if (audit.recent.length === 0) {
      html += '<div class="pf-audit-row"><div class="pf-audit-tool" style="color:#8a8a8e;">Nothing logged yet.</div></div>';
    }
    audit.recent.forEach(function (a) {
      var badgeCls = a.decision === 'denied' || a.decision === 'rejected' ? 'denied' : (a.decision === 'auto_accepted' ? 'auto_accepted' : 'other');
      html += '<div class="pf-audit-row">';
      html += '<div class="pf-audit-connector">' + esc(a.connector) + '</div>';
      html += '<div class="pf-audit-tool">' + esc(a.tool) + '</div>';
      html += '<div class="pf-audit-badge ' + badgeCls + '">' + esc(a.decision) + '</div>';
      html += '<div class="pf-audit-time">' + esc(a.time) + '</div>';
      html += '</div>';
    });
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // About
  // -------------------------------------------------------------------- //

  function renderAbout(state) {
    var about = state.about;
    var html = '<div class="pf-about-page">';
    html += '<div class="pf-about-icon">PF</div>';
    html += '<div class="pf-about-name">PrivacyFence</div>';
    html += '<div class="pf-about-version">Version ' + esc(about.version) + '</div>';
    html += '<div class="pf-about-desc">Human control and policy enforcement for AI access to enterprise data.</div>';
    html += '<div class="pf-about-repo" role="button" tabindex="0" aria-label="Open GitHub repository" ' +
      dataAttr('open_repo', {}) + '>' + esc(about.repo_url.replace('https://', '')) + ' ↗</div>';
    html += '<div class="pf-about-license">' + esc(about.license) + '</div>';
    html += '<div class="pf-about-buttons">';
    html += '<div class="pf-btn-secondary" role="button" tabindex="0" aria-label="Check for Updates" ' +
      dataAttr('check_for_updates', {}) + '>Check for Updates</div>';
    html += '<div class="pf-btn-danger" role="button" tabindex="0" aria-label="Quit PrivacyFence" ' +
      dataAttr('quit_app', {}) + '>Quit PrivacyFence</div>';
    html += '</div></div>';
    return html;
  }

  // -------------------------------------------------------------------- //
  // Top-level render
  // -------------------------------------------------------------------- //

  function renderSection(state) {
    switch (ui.section) {
      case 'connectors': return renderConnectors(state);
      case 'rules': return renderRules(state);
      case 'privacy': return renderPrivacy(state);
      case 'audit': return renderAudit(state);
      case 'about': return renderAbout(state);
      default: return renderGeneral(state);
    }
  }

  // -------------------------------------------------------------------- //
  // Telegram sign-in modal (see settings_controller.py's telegram_start_
  // auth/telegram_submit_code/telegram_submit_2fa/telegram_cancel_auth)
  // -------------------------------------------------------------------- //

  var TELEGRAM_STEP_COPY = {
    phone: {
      title: 'Sign in to Telegram', desc: 'Phone number, with country code (e.g. +1234567890):',
      placeholder: '+1234567890', type: 'text', submitLabel: 'Send Code', submitAction: 'telegram_start_auth', field: 'phone',
    },
    code: {
      title: 'Enter verification code', desc: 'Telegram sent a code to the number above.',
      placeholder: 'Code', type: 'text', submitLabel: 'Authorize', submitAction: 'telegram_submit_code', field: 'code',
    },
    password: {
      title: 'Two-step verification', desc: 'Enter your Telegram two-step verification password.',
      placeholder: 'Password', type: 'password', submitLabel: 'Submit', submitAction: 'telegram_submit_2fa', field: 'password',
    },
  };

  function renderTelegramModal(state) {
    if (!ui.telegramModalOpen) return '';
    var auth = state.telegram_auth || { step: null, error: '' };
    var step = auth.step || 'phone';
    var copy = TELEGRAM_STEP_COPY[step];
    var busy = (state.connectors.find(function (c) { return c.key === 'telegram'; }) || {}).busy;

    var html = '<div class="pf-modal-overlay" role="presentation">';
    html += '<div class="pf-modal" role="dialog" aria-modal="true" aria-label="' + esc(copy.title) + '">';
    html += '<div class="pf-modal-title">' + esc(copy.title) + '</div>';
    html += '<div class="pf-modal-desc">' + esc(copy.desc) + '</div>';
    if (auth.error) html += '<div class="pf-modal-error">' + esc(auth.error) + '</div>';
    html += '<input type="' + copy.type + '" class="pf-input pf-modal-input" placeholder="' + esc(copy.placeholder) +
      '" data-telegram-field="' + copy.field + '" aria-label="' + esc(copy.placeholder) + '"' + (busy ? ' disabled' : '') + '/>';
    html += '<div class="pf-modal-buttons">';
    html += '<div class="pf-btn-secondary" role="button" tabindex="0" aria-label="Cancel Telegram sign-in" data-telegram-cancel="1">Cancel</div>';
    html += '<div class="pf-btn-primary" role="button" tabindex="0" aria-label="' + esc(copy.submitLabel) +
      '" data-telegram-submit="' + copy.submitAction + '"' + (busy ? ' style="opacity:.5;pointer-events:none;"' : '') + '>' +
      (busy ? 'Working…' : esc(copy.submitLabel)) + '</div>';
    html += '</div></div></div>';
    return html;
  }

  function render(state) {
    pyState = state;
    // Auto-close the modal once Python reports the sign-in is no longer in
    // progress (success or explicit cancel) -- see telegram_cancel_auth/
    // the success branches of telegram_submit_code/telegram_submit_2fa.
    // Gated on telegramAuthWasActive (see its own comment above): a modal
    // that's open but has never actually submitted anything also has
    // telegram_auth.step === null, and must not be closed out from under
    // the user before they've even typed a phone number.
    var authStep = state.telegram_auth && state.telegram_auth.step;
    if (authStep) ui.telegramAuthWasActive = true;
    if (ui.telegramModalOpen && ui.telegramAuthWasActive && !authStep && !(state.telegram_auth && state.telegram_auth.error)) {
      ui.telegramModalOpen = false;
      ui.telegramAuthWasActive = false;
    }
    var html = renderNav(state);
    if (state.error) {
      html += '<div class="pf-content" style="flex-direction:column;">' +
        '<div class="pf-error-banner" role="alert"><div>' + esc(state.error) + '</div>' +
        '<div class="pf-error-dismiss" role="button" tabindex="0" aria-label="Dismiss error" data-dismiss-error="1">✕</div></div>' +
        '<div style="flex:1;display:flex;overflow:hidden;">' + renderSection(state) + '</div></div>';
    } else {
      html += '<div class="pf-content">' + renderSection(state) + '</div>';
    }
    html += renderTelegramModal(state);
    document.getElementById('app').innerHTML = html;
    if (ui.telegramModalOpen) {
      var input = document.querySelector('[data-telegram-field]');
      if (input) input.focus();
    }
  }

  window.__pfRender = render;
  window.__pfDebugHook = { ui: ui, render: render, TELEGRAM_STEP_COPY: TELEGRAM_STEP_COPY, onClick: null, onKeydown: null };

  // -------------------------------------------------------------------- //
  // Event delegation
  // -------------------------------------------------------------------- //

  function onClick(e) {
    var navEl = e.target.closest('[data-nav]');
    if (navEl) { ui.section = navEl.getAttribute('data-nav'); render(pyState); return; }

    var rulesNavEl = e.target.closest('[data-rules-nav]');
    if (rulesNavEl) { ui.rulesConnector = rulesNavEl.getAttribute('data-rules-nav'); render(pyState); return; }

    var privacyNavEl = e.target.closest('[data-privacy-nav]');
    if (privacyNavEl) { ui.privacyGroup = privacyNavEl.getAttribute('data-privacy-nav'); render(pyState); return; }

    var dismissEl = e.target.closest('[data-dismiss-error]');
    if (dismissEl) { pyState.error = ''; render(pyState); return; }

    var telegramAuthEl = e.target.closest('[data-telegram-auth]');
    if (telegramAuthEl) { ui.telegramModalOpen = true; ui.telegramAuthWasActive = false; render(pyState); return; }

    var telegramCancelEl = e.target.closest('[data-telegram-cancel]');
    if (telegramCancelEl) {
      ui.telegramModalOpen = false;
      ui.telegramAuthWasActive = false;
      post('telegram_cancel_auth', {});
      render(pyState);
      return;
    }

    var telegramSubmitEl = e.target.closest('[data-telegram-submit]');
    if (telegramSubmitEl) { submitTelegramModal(telegramSubmitEl.getAttribute('data-telegram-submit')); return; }

    var repoEl = e.target.closest('[data-action="open_repo"]');
    if (repoEl) { post('open_repo', {}); return; }

    var actionEl = e.target.closest('[data-action]');
    if (actionEl) {
      var action = actionEl.getAttribute('data-action');
      var payload = {};
      try { payload = JSON.parse(actionEl.getAttribute('data-payload') || '{}'); } catch (err) { payload = {}; }
      post(action, payload);
    }
  }

  function submitTelegramModal(action) {
    var input = document.querySelector('[data-telegram-field]');
    var value = input ? input.value : '';
    var field = input ? input.getAttribute('data-telegram-field') : 'phone';
    var payload = {};
    payload[field] = value;
    post(action, payload);
  }


  function commitRuleField(el) {
    post('update_rule_row', {
      op_key: el.getAttribute('data-op-key'),
      idx: parseInt(el.getAttribute('data-idx'), 10),
      field: el.getAttribute('data-rule-field'),
      value: el.value,
    });
  }

  function commitGrantField(el) {
    post('update_grant_row', {
      connector: el.getAttribute('data-connector'),
      config_key: el.getAttribute('data-config-key'),
      idx: parseInt(el.getAttribute('data-idx'), 10),
      field: el.getAttribute('data-grant-field'),
      value: el.value,
    });
  }

  function onBlur(e) {
    var el = e.target;
    if (!el.tagName || el.tagName !== 'INPUT') return;
    if (el.hasAttribute('data-rule-field')) { commitRuleField(el); return; }
    if (el.hasAttribute('data-grant-field')) { commitGrantField(el); return; }
  }

  function onInput(e) {
    var el = e.target;
    if (el.hasAttribute('data-rules-search')) {
      ui.rulesSearch = el.value;
      var pos = el.selectionStart;
      render(pyState);
      var fresh = document.querySelector('[data-rules-search]');
      if (fresh) { fresh.focus(); try { fresh.setSelectionRange(pos, pos); } catch (err) {} }
    }
  }

  function onKeydown(e) {
    if (e.key === 'Escape' && ui.telegramModalOpen) {
      ui.telegramModalOpen = false;
      ui.telegramAuthWasActive = false;
      post('telegram_cancel_auth', {});
      render(pyState);
      return;
    }
    // Enter/Space activates any of our ARIA-role'd interactive elements
    // (role="button"/"tab"/"radio"/"switch"/"checkbox") the same way a
    // click does -- they're plain <div>s with tabindex="0", not native
    // <button>s, so the browser doesn't do this for free.
    if ((e.key === 'Enter' || e.key === ' ') && e.target.tagName !== 'INPUT' && e.target.closest) {
      var interactive = e.target.closest('[role="button"], [role="tab"], [role="radio"], [role="switch"], [role="checkbox"]');
      if (interactive) {
        e.preventDefault();
        interactive.click();
        return;
      }
    }
    if (e.key !== 'Enter') return;
    var el = e.target;
    if (!el.tagName || el.tagName !== 'INPUT') return;
    if (el.hasAttribute('data-rule-field') || el.hasAttribute('data-grant-field')) {
      el.blur();
      return;
    }
    if (el.hasAttribute('data-telegram-field')) {
      var state = pyState || {};
      var step = ((state.telegram_auth || {}).step) || 'phone';
      submitTelegramModal(TELEGRAM_STEP_COPY[step].submitAction);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.body.addEventListener('click', onClick);
    document.body.addEventListener('blur', onBlur, true);
    document.body.addEventListener('input', onInput);
    document.body.addEventListener('keydown', onKeydown);
    render(window.__pfInitialState);
  });
})();
"""


def build_html(state: dict) -> str:
    """Full self-contained HTML document for the settings window's WKWebView.

    ``state`` is embedded directly as ``window.__pfInitialState`` so the
    first paint needs no round trip to Python -- see this module's
    docstring for the bridge protocol Python's re-renders (``window.
    __pfRender``) follow afterwards.
    """
    state_json = json.dumps(state)
    return (
        "<title>PrivacyFence Settings</title>"
        f"<style>{_CSS}</style>"
        '<div id="app"></div>'
        f"<script>window.__pfInitialState = {state_json};</script>"
        f"<script>{_JS}</script>"
    )
