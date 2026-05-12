"""Event capture: inject a JS observer via Playwright expose_binding."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from openclose.log import get_logger

log = get_logger(__name__)


_BINDING_NAME = "__openclose_recorder"

_OBSERVER_JS = r"""
(() => {
  if (window.__openclose_recorder_installed) return;
  window.__openclose_recorder_installed = true;

  function send(evt) {
    try {
      if (typeof window.__openclose_recorder === 'function') {
        window.__openclose_recorder(JSON.stringify(evt));
      }
    } catch (e) {}
  }

  // Emit the current URL on every document load, but only from the top
  // frame — otherwise iframe loads (reCAPTCHA, ads, about:blank)
  // pollute the log. The install-guard above makes this fire exactly
  // once per document.
  if (window === window.top) {
    send({ type: 'navigate', url: location.href });
  }

  function visibleLabel(el) {
    if (!el) return '';
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 200);
    const text = (el.innerText || el.textContent || '').trim();
    if (text) return text.slice(0, 200);
    const title = el.getAttribute && el.getAttribute('title');
    if (title) return title.trim().slice(0, 200);
    const alt = el.getAttribute && el.getAttribute('alt');
    if (alt) return alt.trim().slice(0, 200);
    return '';
  }

  function fieldLabel(el) {
    if (!el) return '';
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) {
        const t = (lab.innerText || lab.textContent || '').trim();
        if (t) return t.slice(0, 200);
      }
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim().slice(0, 200);
    const ph = el.getAttribute('placeholder');
    if (ph) return ph.trim().slice(0, 200);
    const name = el.getAttribute('name');
    if (name) return name.trim().slice(0, 200);
    let parent = el.parentElement;
    for (let i = 0; i < 3 && parent; i++) {
      if (parent.tagName === 'LABEL') {
        const t = (parent.innerText || parent.textContent || '').trim();
        if (t) return t.slice(0, 200);
      }
      parent = parent.parentElement;
    }
    return '';
  }

  function rectOf(el) {
    try {
      const r = el.getBoundingClientRect();
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    } catch (e) { return null; }
  }

  document.addEventListener('click', (e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    send({
      type: 'click',
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute && el.getAttribute('role') || '',
      label: visibleLabel(el),
      rect: rectOf(el),
      url: location.href,
    });
  }, true);

  function recordInputValue(el) {
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return;
    const type = (el.getAttribute('type') || '').toLowerCase();
    let value;
    if (type === 'password') {
      value = '<redacted>';
    } else if (tag === 'select') {
      value = el.value;
    } else {
      value = (el.value || '').slice(0, 500);
    }
    send({
      type: 'input',
      tag: tag,
      input_type: type,
      label: fieldLabel(el),
      value: value,
      url: location.href,
    });
  }

  document.addEventListener('change', (e) => recordInputValue(e.target), true);
  document.addEventListener('blur', (e) => {
    const el = e.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea') recordInputValue(el);
  }, true);

  function isInPasswordField(node) {
    try {
      let el = node;
      if (el && el.nodeType === 3) el = el.parentElement;
      while (el) {
        if (el.tagName === 'INPUT' &&
            (el.getAttribute('type') || '').toLowerCase() === 'password') {
          return true;
        }
        el = el.parentElement;
      }
    } catch (e) {}
    return false;
  }

  function selectionRect(sel) {
    try {
      if (!sel || sel.rangeCount === 0) return null;
      const r = sel.getRangeAt(0).getBoundingClientRect();
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
    } catch (e) { return null; }
  }

  let __lastSelText = '';
  function emitSelection() {
    try {
      const sel = window.getSelection();
      if (!sel) return;
      const text = (sel.toString() || '').trim();
      if (!text) { __lastSelText = ''; return; }
      if (text === __lastSelText) return;
      __lastSelText = text;
      const redact = isInPasswordField(sel.anchorNode) || isInPasswordField(sel.focusNode);
      send({
        type: 'select',
        text: redact ? '<redacted>' : text.slice(0, 500),
        rect: selectionRect(sel),
        url: location.href,
      });
    } catch (e) {}
  }

  document.addEventListener('mouseup', () => setTimeout(emitSelection, 0), true);
  document.addEventListener('keyup', (e) => {
    if (e.shiftKey || e.key === 'Shift' ||
        (e.ctrlKey && (e.key === 'a' || e.key === 'A'))) {
      setTimeout(emitSelection, 0);
    }
  }, true);

  function copiedText(e) {
    // In a copy/cut event, clipboardData.getData() is empty unless the
    // handler called setData() itself — the browser fills the clipboard
    // from the current selection after the event. Read the selection
    // directly. For <input>/<textarea>, window.getSelection() is empty,
    // so fall back to the target's selectionStart/End.
    try {
      const sel = (window.getSelection() || {}).toString() || '';
      if (sel) return sel;
      const el = e.target;
      if (el && typeof el.selectionStart === 'number' &&
          typeof el.selectionEnd === 'number' &&
          el.selectionEnd > el.selectionStart) {
        return (el.value || '').slice(el.selectionStart, el.selectionEnd);
      }
    } catch (err) {}
    return '';
  }

  function onCopyCut(kind) {
    return (e) => {
      const text = copiedText(e);
      if (!text) return;
      const redact = isInPasswordField(e.target) ||
                     isInPasswordField((window.getSelection() || {}).anchorNode);
      send({
        type: kind,
        text: redact ? '<redacted>' : text.slice(0, 500),
        url: location.href,
      });
    };
  }

  document.addEventListener('copy', onCopyCut('copy'), true);
  document.addEventListener('cut', onCopyCut('cut'), true);

  document.addEventListener('paste', (e) => {
    let text = '';
    try {
      if (e.clipboardData) text = e.clipboardData.getData('text/plain') || '';
    } catch (err) {}
    if (!text) return;
    const redact = isInPasswordField(e.target);
    send({
      type: 'paste',
      text: redact ? '<redacted>' : text.slice(0, 500),
      target_label: fieldLabel(e.target),
      url: location.href,
    });
  }, true);
})();
"""


@dataclass
class EventLog:
    """Collects events from the injected observer and page navigations."""

    page: Any  # playwright Page
    cdp: Any = None  # playwright CDPSession, used for main-frame URL polling
    started_at: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    _last_nav_url: str = ""
    _last_click_t: float | None = None
    _poll_task: Any = None
    _poll_stop: Any = None
    _frame_nav_handler: Any = None
    _binding_handler: Any = None
    _loop: Any = None

    async def start(self) -> None:
        self.started_at = time.monotonic()

        # Each Playwright setup call is wrapped independently so a single
        # stall (unresponsive renderer, chrome:// page, already-installed
        # binding) can't block the recorder from starting. Screencast-only
        # recording is a valid fallback.
        try:
            await asyncio.wait_for(
                self.page.expose_binding(
                    _BINDING_NAME,
                    lambda source, payload: self._on_binding(payload),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.warning("expose_binding timed out — click/input events disabled")
        except Exception as e:  # noqa: BLE001
            log.debug("expose_binding failed (already installed?): %s", e)

        try:
            await asyncio.wait_for(
                self.page.add_init_script(_OBSERVER_JS),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.warning("add_init_script timed out — future pages won't report events")
        except Exception as e:  # noqa: BLE001
            log.debug("add_init_script failed: %s", e)

        try:
            await asyncio.wait_for(
                self.page.evaluate(_OBSERVER_JS),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.warning("initial observer install timed out")
        except Exception as e:  # noqa: BLE001
            log.debug("initial observer install eval failed: %s", e)

        # Under connect_over_cdp, Playwright's add_init_script isn't
        # reliably re-injecting on main-frame navigations initiated from
        # the browser chrome (bookmarks, address bar). Subscribe to
        # CDP Page.frameNavigated and re-inject the observer manually
        # on every main-frame commit. Poll loop is kept as a
        # belt-and-suspenders fallback.
        if self.cdp is not None:
            self._loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(self.cdp.send("Page.enable"), timeout=2.0)
                self._frame_nav_handler = self._on_frame_navigated
                self.cdp.on("Page.frameNavigated", self._frame_nav_handler)
            except asyncio.TimeoutError:
                log.warning("Page.enable timed out — observer won't re-inject on navigation")
            except Exception as e:  # noqa: BLE001
                log.debug("Page.frameNavigated subscription failed: %s", e)

            # Register the binding directly via CDP. Playwright's
            # expose_binding relies on add_init_script to expose the
            # function on every new document, which isn't reliable under
            # connect_over_cdp. Runtime.addBinding surfaces the function
            # in every execution context (including fresh documents after
            # address-bar navigations) with no init script needed.
            try:
                await asyncio.wait_for(self.cdp.send("Runtime.enable"), timeout=2.0)
                await asyncio.wait_for(
                    self.cdp.send("Runtime.addBinding", {"name": _BINDING_NAME}),
                    timeout=2.0,
                )
                self._binding_handler = self._on_cdp_binding_called
                self.cdp.on("Runtime.bindingCalled", self._binding_handler)
            except asyncio.TimeoutError:
                log.warning("Runtime.addBinding timed out — events may be lost on new documents")
            except Exception as e:  # noqa: BLE001
                log.debug("Runtime.addBinding failed: %s", e)

        self._poll_stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _on_frame_navigated(self, params: dict[str, Any]) -> None:
        """CDP callback (sync): schedule an async observer re-injection.

        Fires for every frame navigation commit. We only care about the
        top frame (no parentId) and skip non-web schemes where the
        observer can't run or isn't useful.
        """
        try:
            frame = params.get("frame", {}) if isinstance(params, dict) else {}
            if frame.get("parentId"):
                return
            url = frame.get("url", "") or ""
            if not url:
                return
            if url.startswith(("about:", "chrome:", "devtools:", "chrome-extension:")):
                return
            if self._loop is None:
                return
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._reinject_observer())
            )
        except Exception as e:  # noqa: BLE001
            log.debug("_on_frame_navigated error: %s", e)

    async def _reinject_observer(self) -> None:
        if self.cdp is None:
            return
        try:
            await asyncio.wait_for(
                self.cdp.send("Runtime.evaluate", {"expression": _OBSERVER_JS}),
                timeout=2.0,
            )
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            log.debug("observer re-inject failed: %s", e)

    def _on_cdp_binding_called(self, params: dict[str, Any]) -> None:
        """CDP callback for Runtime.bindingCalled — dispatch to _on_binding."""
        try:
            if not isinstance(params, dict):
                return
            if params.get("name") != _BINDING_NAME:
                return
            payload = params.get("payload") or ""
            self._on_binding(payload)
        except Exception as e:  # noqa: BLE001
            log.debug("_on_cdp_binding_called error: %s", e)

    async def _get_top_url(self) -> str:
        """Ask Chrome directly for the top frame's URL.

        Page.getFrameTree returns the full frame hierarchy rooted at the
        main frame; using this avoids Playwright's main_frame / page.url
        quirks under connect_over_cdp, where evaluate() sometimes runs in
        (or reports) a subframe context.
        """
        if self.cdp is None:
            return ""
        try:
            tree = await asyncio.wait_for(
                self.cdp.send("Page.getFrameTree"),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            log.debug("Page.getFrameTree failed: %s", e)
            return ""
        try:
            return tree["frameTree"]["frame"].get("url", "") or ""
        except Exception:
            return ""

    async def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                url = await self._get_top_url()
                if url and url != self._last_nav_url:
                    self._last_nav_url = url
                    t_now = round(time.monotonic() - self.started_at, 3)
                    if (
                        self._last_click_t is not None
                        and t_now - self._last_click_t <= 0.8
                    ):
                        origin = "in_page_click"
                    else:
                        origin = "external"
                    self.events.append({
                        "type": "navigate",
                        "url": url,
                        "origin": origin,
                        "t": t_now,
                    })
                    # Re-inject the observer on the new document. The
                    # observer's own __openclose_recorder_installed guard
                    # prevents duplicate listeners if add_init_script
                    # already ran.
                    if not url.startswith((
                        "about:", "chrome:", "devtools:", "chrome-extension:"
                    )):
                        asyncio.create_task(self._reinject_observer())
            except asyncio.TimeoutError:
                pass
            except Exception as e:  # noqa: BLE001
                log.debug("poll_loop error: %s", e)
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

    def _on_binding(self, payload: str) -> None:
        try:
            evt = json.loads(payload)
        except Exception:
            return
        evt["t"] = round(time.monotonic() - self.started_at, 3)
        evt_type = evt.get("type")
        if evt_type == "click":
            self._last_click_t = evt["t"]
        elif evt_type == "navigate":
            url = evt.get("url", "") or ""
            if not url or url == self._last_nav_url:
                return  # dedup repeated identical URLs (e.g. hash churn)
            self._last_nav_url = url
            if (
                self._last_click_t is not None
                and evt["t"] - self._last_click_t <= 0.8
            ):
                evt["origin"] = "in_page_click"
            else:
                evt["origin"] = "external"
        self.events.append(evt)

    async def stop(self) -> None:
        if self._frame_nav_handler is not None and self.cdp is not None:
            try:
                self.cdp.remove_listener("Page.frameNavigated", self._frame_nav_handler)
            except Exception as e:  # noqa: BLE001
                log.debug("remove frameNavigated listener failed: %s", e)
            self._frame_nav_handler = None
        if self._binding_handler is not None and self.cdp is not None:
            try:
                self.cdp.remove_listener("Runtime.bindingCalled", self._binding_handler)
            except Exception as e:  # noqa: BLE001
                log.debug("remove bindingCalled listener failed: %s", e)
            self._binding_handler = None
        if self._poll_stop is not None:
            self._poll_stop.set()
        if self._poll_task is not None:
            try:
                await asyncio.wait_for(self._poll_task, timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
