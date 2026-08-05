"""Small-dialog HTML template for the confirmation/list-picker windows
dialog_window.py hosts -- ports approval_popup.py's old AppleScript
`display dialog`/`choose from list` prompts onto issue #141's
AppKit+WKWebView bridge pattern (issue #145). Reuses approval_window_html.py's
vendored ``styles.css`` (design tokens, embedded fonts, the
``.pf-btn``/``.pf-btn-primary``/``.pf-btn-deny`` button styles, ``.pf-scroll``'s
scrollbar styling) rather than a second copy of the same visual language --
these are just much smaller, fixed-shape documents than
``build_card_stack_html``'s full card stack, with no header icon/pill, no
preview pane, no PII/disclosure cards.

Two shapes:
  - ``build_confirmation_html()``: a two-button Cancel/<confirm_label> row --
    the shape both ``show_pii_confirmation_popup`` and
    ``show_rule_confirmation_popup`` render, with different title/copy/
    confirm_label but otherwise identical. Cancel is the safe default: the
    accepting button carries ``data-pf-primary`` (the same attribute
    ``approval_window_html.py``'s own Allow once button uses), which
    ``_JS``'s keydown handler deliberately excludes from the Enter/Space-
    activates-a-focused-control path -- hitting Enter can never silently
    accept. Escape resolves Cancel from anywhere in the document, matching
    ``_display_dialog``'s old default-button-is-Cancel contract exactly.
  - ``build_choice_html()``: a vertical list of clickable option rows plus a
    Cancel button -- the shape both ``show_rule_choice_popup`` and
    ``settings_controller._osascript_pick``'s Atlassian multi-resource
    picker render. Escape or Cancel resolve to no selection, matching
    ``_run``'s old "non-zero osascript exit returns None" contract (see
    ``dialog_window.py``'s ``show_choice_dialog`` for where that None
    actually gets produced from the bridge's own result).

Bridge protocol (JS -> Python only, same shape as approval_window_html.py's
own): the page posts
``window.webkit.messageHandlers.pf.postMessage({action: 'resolve', result})``
once a button/option resolves the dialog. ``result`` is ``'cancel'``/
``'confirm'`` for the confirmation shape, or a chosen option's index (a
number) / ``'cancel'`` for the choice shape.

Every value interpolated into these documents -- button labels, dialog copy,
and (for the choice shape) each option's own display text -- is run through
``_html_escape()`` before interpolation, the same defensive posture
``build_card_stack_html`` takes with ``details_text``. This is a real fix,
not just a precaution: ``_osascript_pick``'s options previously went
unescaped into AppleScript source text (a real OAuth ``accessible-resources``
URL containing a literal quote could break out of the string literal); a
webview bridge call takes the string as a real DOM text value, never source
text to be interpreted, so that injection class doesn't exist here at all --
escaping is still applied to keep the HTML itself well-formed, not to guard
against script execution.
"""
from __future__ import annotations

from html import escape as _html_escape

from .approval_window_html import _STYLES_CSS

# Public (no leading underscore): dialog_window.py's own window-width
# constants derive from these directly rather than duplicating them, so the
# native window frame and the HTML body rendered inside it can never drift
# out of sync -- same discipline approval_window.py's _WINDOW_WIDTH takes
# with approval_window_html.CONTENT_WIDTH.
CONFIRM_WIDTH = 440
PICKER_WIDTH = 480

# Click/keyboard dispatch, plus the "content is actually ready" gate -- the
# same DOMContentLoaded-is-the-right-signal reasoning as approval_window_
# html.py's own _JS (nothing here ever fetches anything either: fonts/colors
# come from the same already-inlined styles.css, and there are no images at
# all in these two shapes). window.__pfEnableButtons is exposed for the same
# reason too: dialog_window.py's WKNavigationDelegate fail-safes can force
# button click-ability if DOMContentLoaded itself never fires.
_JS = """
(function () {
  function post(result) {
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.pf) {
      window.webkit.messageHandlers.pf.postMessage({ action: 'resolve', result: result });
    }
  }

  function resultFor(el) {
    if (el.getAttribute('data-pf-action') === 'choice') {
      return parseInt(el.getAttribute('data-pf-index'), 10);
    }
    return el.getAttribute('data-pf-action');
  }

  function resolveFrom(el) {
    if (!el || el.getAttribute('aria-disabled') === 'true') return;
    if (!el.getAttribute('data-pf-action')) return;
    post(resultFor(el));
  }

  function enableButtons() {
    var controls = document.querySelectorAll('[data-pf-action]');
    for (var i = 0; i < controls.length; i++) {
      controls[i].removeAttribute('aria-disabled');
      controls[i].setAttribute('tabindex', '0');
    }
  }
  window.__pfEnableButtons = enableButtons;

  document.addEventListener('DOMContentLoaded', function () {
    enableButtons();

    document.body.addEventListener('click', function (e) {
      resolveFrom(e.target.closest('[data-pf-action]'));
    });

    document.body.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        // Resolves Cancel from anywhere, not just when focused -- declining
        // via a reflexive keypress is always the safe direction. Present on
        // both shapes: build_confirmation_html's Cancel button and build_
        // choice_html's own Cancel button both carry data-pf-action="cancel".
        resolveFrom(document.querySelector('[data-pf-action="cancel"]'));
        return;
      }
      // See build_confirmation_html's own docstring for why data-pf-primary
      // (the accepting button) is deliberately excluded here -- mirrors
      // approval_window_html.py's own Allow once exclusion.
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest) {
        var interactive = e.target.closest('[data-pf-action]:not([data-pf-primary])');
        if (interactive) {
          e.preventDefault();
          resolveFrom(interactive);
        }
      }
    });
  });
})();
"""


def _confirm_button_row_html(cancel_label: str, confirm_label: str) -> str:
    """Cancel (left) / <confirm_label> (right, primary) -- same left/right
    grouping as approval_window_html.py's own _button_row_html (Deny left,
    Allow once right)."""
    cancel_html = (
        '<div class="pf-btn pf-btn-deny" role="button" aria-disabled="true" '
        f'aria-label="{_html_escape(cancel_label)}" data-pf-action="cancel">{_html_escape(cancel_label)}</div>'
    )
    confirm_html = (
        '<div class="pf-btn pf-btn-primary" role="button" aria-disabled="true" '
        f'data-pf-primary="1" aria-label="{_html_escape(confirm_label)}" data-pf-action="confirm">'
        f'{_html_escape(confirm_label)}</div>'
    )
    return f'<div class="pf-btn-row"><div class="pf-btn-row-left">{cancel_html}</div>{confirm_html}</div>'


def _cancel_only_button_row_html(cancel_label: str) -> str:
    cancel_html = (
        '<div class="pf-btn pf-btn-deny" role="button" aria-disabled="true" '
        f'aria-label="{_html_escape(cancel_label)}" data-pf-action="cancel">{_html_escape(cancel_label)}</div>'
    )
    return f'<div class="pf-btn-row"><div class="pf-btn-row-left">{cancel_html}</div></div>'


def _message_html(lines: list[str]) -> str:
    """Each non-empty line becomes its own paragraph. Empty lines (the old
    AppleScript ``lines`` lists used them purely as inter-paragraph spacing,
    see approval_popup.py's former ``_build_message``) are dropped rather
    than rendered as an empty ``<p>`` -- normal CSS paragraph margin already
    provides that spacing here."""
    return "".join(f"<p>{_html_escape(line)}</p>" for line in lines if line)


def _document(*, width: int, body_html: str) -> str:
    """Shared page shell for both shapes -- same overall structure as
    approval_window_html.build_card_stack_html's own returned document
    (vendored styles.css, a couple of small overrides, the bridge script),
    just without that function's per-layout width/rail-color logic, since
    both shapes here are one fixed narrow width apiece."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<style>
{_STYLES_CSS}
html {{ height: 100%; }}
html, body {{ overflow-y: auto; }}
body {{
  box-sizing: border-box; width: {width}px; height: 100vh;
  padding: 24px 28px;
  display: flex; flex-direction: column;
}}
h2 {{ font-size: 19px; margin-bottom: 12px; }}
.pf-choice-list {{
  display: flex; flex-direction: column; gap: 6px;
  flex: 1; min-height: 0; overflow-y: auto;
  margin: 4px 0 var(--space-3);
}}
.pf-choice-row {{
  padding: 10px 12px; border-radius: var(--radius-md);
  background: var(--color-surface); font-size: 13px;
  cursor: pointer; user-select: none;
}}
.pf-choice-row:hover {{ background: color-mix(in srgb, var(--color-accent) 12%, var(--color-surface)); }}
.pf-choice-row[aria-disabled="true"] {{ opacity: .45; pointer-events: none; cursor: default; }}
</style>
</head>
<body>{body_html}<script>{_JS}</script></body>
</html>
"""


def build_confirmation_html(
    *, title: str, message_lines: list[str], cancel_label: str, confirm_label: str,
) -> str:
    """Two-button Cancel/<confirm_label> dialog. See module docstring for
    the Cancel-is-default security behavior this preserves from
    ``_display_dialog``."""
    body_html = (
        '<div class="pf-kicker"><span>PrivacyFence</span></div>'
        f'<h2>{_html_escape(title)}</h2>'
        f'<div style="flex:1;min-height:0;overflow-y:auto">{_message_html(message_lines)}</div>'
        f'{_confirm_button_row_html(cancel_label, confirm_label)}'
    )
    return _document(width=CONFIRM_WIDTH, body_html=body_html)


def build_choice_html(
    *, title: str, prompt: str, options: list[str], cancel_label: str = "Cancel",
) -> str:
    """A vertical list of clickable option rows plus Cancel. See module
    docstring for the escape/cancel-returns-no-selection contract this
    preserves from ``_run``'s old "non-zero osascript exit" behavior."""
    rows = "".join(
        '<div class="pf-choice-row" role="button" aria-disabled="true" '
        f'aria-label="{_html_escape(opt)}" data-pf-action="choice" data-pf-index="{i}">'
        f'{_html_escape(opt)}</div>'
        for i, opt in enumerate(options)
    )
    body_html = (
        '<div class="pf-kicker"><span>PrivacyFence</span></div>'
        f'<h2>{_html_escape(title)}</h2>'
        f'<p>{_html_escape(prompt)}</p>'
        f'<div class="pf-choice-list">{rows}</div>'
        f'{_cancel_only_button_row_html(cancel_label)}'
    )
    return _document(width=PICKER_WIDTH, body_html=body_html)
