"""Mobile terminal input and selection controls fragment."""


def render_input_controls() -> str:
    return f"""        // ---- Mobile scrolling: native inertia ----
        // xterm.js registers touchstart/touchmove on the .xterm element (parent).
        // Its handleTouchMove does scrollTop += delta manually — no inertia.
        // Its _innerRefresh snaps scrollTop = ydisp * rowHeight each frame.
        //
        // Strategy:
        // 1. Override viewport.handleTouchMove/handleTouchStart to no-ops so
        //    xterm's listener callback does nothing useful even if it fires
        // 2. Let browser-native scroll on .xterm-viewport (overflow-y:scroll) work
        // 3. Keep xterm's _innerRefresh intact. Live output can advance
        //    viewportY/baseY while the user is touching history; blocking
        //    _innerRefresh lets scrollTop go stale and makes rendered
        //    history appear mixed with new output until touchend.
        var _isTouchScrolling = false;
        var _touchScrollTimer = null;

        function _getTerm() {{
          var term = window.ttyd && window.ttyd.terminal ? window.ttyd.terminal : window.term;
          return term || null;
        }}

        function _getViewportObj() {{
          var term = _getTerm();
          return term ? (term.viewport || (term._core && term._core.viewport) || null) : null;
        }}

        // ---- Mobile "select text" mode ----
        // xterm.js v4 has no native touch text-selection: touch only scrolls.
        // On touch devices we keep passthrough scrolling by default (see the
        // pointer:coarse CSS above) and let the user opt into selection via an
        // explicit toggle (MobileControls "选择" button -> terminal-select-mode
        // message). While active, single-finger touches are translated into the
        // synthetic mouse drag that xterm's own SelectionService understands,
        // so selection semantics (partial first/last row, multi-row) match the
        // desktop mouse exactly. On lift, the selection is copied to the
        // clipboard and the parent is notified for a toast.
        var _selectModeActive = false;
        var _selTouchActive = false;

        var _selAnchor = null;

        // ---- Long-press to select (no mode toggle required) ----
        // The explicit "选择" toggle above still works, but the natural mobile
        // gesture is a long-press: hold a finger still on the text for a moment
        // to start a selection, drag to extend, lift to copy. We arm a timer on
        // touchstart and only activate selection if the finger stays roughly
        // still until it fires; any early movement cancels the timer so a normal
        // swipe still scrolls with native inertia.
        var LONG_PRESS_MS = 450;
        var LONG_PRESS_MOVE_TOLERANCE_PX = 10;
        var _longPressTimer = null;
        var _longPressSelectionShown = false;
        var _touchDown = false;
        var _touchStartX = 0;
        var _touchStartY = 0;

        function _screenEl() {{
          return document.querySelector('.xterm-screen');
        }}

        // Map a viewport (clientX, clientY) point to an absolute buffer cell
        // [col, absRow]. xterm.js v4 ignores synthetic MouseEvents for
        // selection (it trusts real input only), so touch selection drives the
        // selection model directly from computed cells instead.
        function _cellFromPoint(clientX, clientY) {{
          var term = _getTerm();
          var screen = _screenEl();
          if (!term || !screen) return null;
          var rect = screen.getBoundingClientRect();
          var core = term._core;
          var dims = core && core._renderService && core._renderService.dimensions;
          var cw = dims && dims.css && dims.css.cell ? dims.css.cell.width : (dims ? dims.actualCellWidth : 0);
          var ch = dims && dims.css && dims.css.cell ? dims.css.cell.height : (dims ? dims.actualCellHeight : 0);
          if (!cw || !ch) return null;
          var cols = term.cols || 80;
          var rows = term.rows || 24;
          var col = Math.floor((clientX - rect.left) / cw);
          var srow = Math.floor((clientY - rect.top) / ch);
          col = Math.max(0, Math.min(cols - 1, col));
          srow = Math.max(0, Math.min(rows - 1, srow));
          var vy = term.buffer && term.buffer.active ? term.buffer.active.viewportY : 0;
          return [col, vy + srow];
        }}

        // Apply a selection from anchor cell to focus cell via the selection
        // model, ordering endpoints so a backward (upward) drag still works.
        function _applySelection(anchor, focus) {{
          var term = _getTerm();
          var ss = term && term._core && term._core._selectionService;
          if (!ss || !ss._model || !anchor || !focus) return;
          var start = anchor;
          var end = focus;
          if (end[1] < start[1] || (end[1] === start[1] && end[0] < start[0])) {{
            start = focus;
            end = anchor;
          }}
          ss._model.selectionStart = [start[0], start[1]];
          // selectionEnd is exclusive on the column, so +1 includes the focused
          // cell under the finger.
          ss._model.selectionEnd = [end[0] + 1, end[1]];
          if (typeof ss.refresh === 'function') ss.refresh();
        }}

        function _fallbackCopy(text) {{
          try {{
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.top = '-1000px';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            var ok = document.execCommand('copy');
            document.body.removeChild(ta);
            return ok;
          }} catch (error) {{
            return false;
          }}
        }}

        function _notifyParentSelect(status, length) {{
          try {{
            var target = window.parent && window.parent !== window ? window.parent : window;
            target.postMessage({{
              type: 'terminal-select-copied',
              tabId: TAB_ID,
              status: status,
              length: length || 0,
            }}, '*');
          }} catch (error) {{}}
        }}

        function copyCurrentSelection() {{
          var term = _getTerm();
          var text = term && typeof term.getSelection === 'function' ? term.getSelection() : '';
          if (!text) {{
            _notifyParentSelect('empty', 0);
            return;
          }}
          var finish = function(ok) {{
            _notifyParentSelect(ok ? 'copied' : 'failed', text.length);
          }};
          try {{
            if (navigator.clipboard && navigator.clipboard.writeText) {{
              navigator.clipboard.writeText(text).then(function() {{
    finish(true);
              }}, function() {{
    finish(_fallbackCopy(text));
              }});
              return;
            }}
          }} catch (error) {{}}
          finish(_fallbackCopy(text));
        }}

        function setSelectMode(on) {{
          _selectModeActive = !!on;
          try {{
            if (_selectModeActive) {{
              document.documentElement.setAttribute('data-select-mode', 'on');
            }} else {{
              document.documentElement.removeAttribute('data-select-mode');
              _selTouchActive = false;
              var term = _getTerm();
              if (term && typeof term.clearSelection === 'function') {{
    term.clearSelection();
              }}
            }}
          }} catch (error) {{}}
        }}

        function _cancelLongPress() {{
          if (_longPressTimer !== null) {{
            clearTimeout(_longPressTimer);
            _longPressTimer = null;
          }}
        }}

        // Select the whole word under a cell for a useful long-press-then-lift
        // (copying a single character is rarely what the user wants). Returns
        // true when a non-empty word selection was produced; the caller falls
        // back to a single-cell anchor otherwise. Dragging afterwards extends
        // the selection cell-by-cell from the anchor, matching select-mode drag.
        function _selectWordAtCell(cell) {{
          try {{
            var term = _getTerm();
            var ss = term && term._core && term._core._selectionService;
            // Prefer xterm's own word selection when this build exposes it
            // (handles wide glyphs correctly).
            if (ss && typeof ss.selectWordAt === 'function') {{
              ss.selectWordAt([cell[0], cell[1]], false);
              if (typeof ss.refresh === 'function') ss.refresh();
              if (term && typeof term.getSelection === 'function' && term.getSelection()) {{
    return true;
              }}
            }}
            // Manual fallback: derive the word span from the buffer line text.
            var buf = term && term.buffer && term.buffer.active;
            var line = buf && typeof buf.getLine === 'function' ? buf.getLine(cell[1]) : null;
            if (!line || typeof line.translateToString !== 'function') return false;
            var text = line.translateToString(true);
            var col = cell[0];
            if (col < 0 || col >= text.length) return false;
            // Cell index only maps 1:1 to the string without wide/non-latin
            // glyphs; skip word selection on such lines so we never highlight
            // the wrong span (cell-precise drag selection still works there).
            if (/[^\\x00-\\xff]/.test(text)) return false;
            var isWord = function(c) {{ return !!c && !/\\s/.test(c); }};
            if (!isWord(text.charAt(col))) return false;
            var start = col;
            var end = col;
            while (start > 0 && isWord(text.charAt(start - 1))) start--;
            while (end < text.length - 1 && isWord(text.charAt(end + 1))) end++;
            _selAnchor = [start, cell[1]];
            _applySelection([start, cell[1]], [end, cell[1]]);
            return true;
          }} catch (error) {{}}
          return false;
        }}

        function _activateLongPressSelection() {{
          _longPressTimer = null;
          if (!_touchDown || _selectModeActive) return;
          var cell = _cellFromPoint(_touchStartX, _touchStartY);
          if (!cell) return;
          _longPressSelectionShown = true;
          _selTouchActive = true;
          _selAnchor = cell;
          // Select the whole word under the finger so a long-press-then-lift
          // copies something useful; drag afterwards extends from there.
          if (!_selectWordAtCell(cell)) {{
            _applySelection(cell, cell);
          }}
          // Light haptic confirmation where supported (Android). Best-effort.
          try {{ if (navigator.vibrate) navigator.vibrate(15); }} catch (error) {{}}
        }}

        function selectTouchStart(event) {{
          if (!event.touches || event.touches.length !== 1) return;
          var t = event.touches[0];

          // Explicit select mode: keep the original tap-to-anchor behavior.
          if (_selectModeActive) {{
            var cell = _cellFromPoint(t.clientX, t.clientY);
            if (!cell) return;
            _selTouchActive = true;
            _selAnchor = cell;
            _applySelection(cell, cell);
            event.preventDefault();
            event.stopPropagation();
            return;
          }}

          // Implicit long-press path. Do NOT preventDefault here so a swipe can
          // still start a native scroll; the timer only fires on a still hold.
          if (_longPressSelectionShown) {{
            // Clear a lingering long-press highlight when a new touch begins.
            _longPressSelectionShown = false;
            var prevTerm = _getTerm();
            if (prevTerm && typeof prevTerm.clearSelection === 'function') {{
              try {{ prevTerm.clearSelection(); }} catch (error) {{}}
            }}
          }}
          _touchDown = true;
          _touchStartX = t.clientX;
          _touchStartY = t.clientY;
          _cancelLongPress();
          _longPressTimer = setTimeout(_activateLongPressSelection, LONG_PRESS_MS);
        }}

        function selectTouchMove(event) {{
          if (!event.touches || event.touches.length !== 1) return;
          var t = event.touches[0];

          // Arming phase: a moved finger means "scroll", not "select".
          if (_longPressTimer !== null && !_selTouchActive) {{
            var dx = Math.abs(t.clientX - _touchStartX);
            var dy = Math.abs(t.clientY - _touchStartY);
            if (dx > LONG_PRESS_MOVE_TOLERANCE_PX || dy > LONG_PRESS_MOVE_TOLERANCE_PX) {{
              _cancelLongPress();
            }}
            return; // let native scroll proceed
          }}

          if (!_selTouchActive) return;
          var moveCell = _cellFromPoint(t.clientX, t.clientY);
          if (!moveCell || !_selAnchor) return;
          _applySelection(_selAnchor, moveCell);
          event.preventDefault();
          event.stopPropagation();
        }}

        function selectTouchEnd(event) {{
          _touchDown = false;
          _cancelLongPress();
          if (!_selTouchActive) return;
          _selTouchActive = false;
          copyCurrentSelection();
          event.preventDefault();
          event.stopPropagation();
        }}

        function _hookSelectTouch(xtermEl) {{
          if (!xtermEl || xtermEl.__claudeHubSelectHooked) return;
          xtermEl.__claudeHubSelectHooked = true;
          // Capture phase + non-passive so we run before (and can suppress) the
          // native-inertia scroll listeners on the viewport while selecting.
          xtermEl.addEventListener('touchstart', selectTouchStart, {{ capture: true, passive: false }});
          xtermEl.addEventListener('touchmove', selectTouchMove, {{ capture: true, passive: false }});
          xtermEl.addEventListener('touchend', selectTouchEnd, {{ capture: true, passive: false }});
          xtermEl.addEventListener('touchcancel', selectTouchEnd, {{ capture: true, passive: false }});
        }}

        function _markUserScrolling() {{
          var term = _getTerm();
          var bufferService = term && term._core && term._core._bufferService;
          if (bufferService) {{
            bufferService.isUserScrolling = true;
          }}
        }}

        function _scrollAwayFromBottom(deltaY) {{
          var term = _getTerm();
          var buffer = term && term.buffer && term.buffer.active;
          if (!term || !buffer || buffer.viewportY < buffer.baseY - 1) return;

          var vpObj = _getViewportObj();
          var rowHeight = (vpObj && vpObj._currentRowHeight) || 15;
          var lines = Math.max(1, Math.ceil(Math.abs(deltaY) / rowHeight));
          if (typeof term.scrollLines === 'function') {{
            term.scrollLines(-lines);
            return;
          }}

          var viewportEl = document.querySelector('.xterm-viewport');
          if (viewportEl) {{
            viewportEl.scrollTop = Math.max(0, viewportEl.scrollTop - lines * rowHeight);
          }}
        }}

        function enableNativeTouchScroll() {{
          var vpObj = _getViewportObj();
          var viewportEl = document.querySelector('.xterm-viewport');
          var xtermEl = document.querySelector('.xterm');
          if (!vpObj || !viewportEl) return false;
          if (vpObj.__claudeHubNativeTouch) return true;
          vpObj.__claudeHubNativeTouch = true;

          // 1. Neutralize xterm's touch handlers on the viewport object.
          //    xterm's touchmove listener on .xterm calls viewport.handleTouchMove()
          //    which does scrollTop += delta. Replace with no-op.
          vpObj.handleTouchStart = function() {{}};
          vpObj.handleTouchMove = function() {{ return true; }};

          // Wire the explicit "select text" touch handlers onto the .xterm root
          // so they run in the capture phase before native scrolling.
          _hookSelectTouch(xtermEl);

          // Desktop wheel/trackpad scrolls can race with fast live output
          // while the viewport is still at the bottom. Mark user scrolling
          // in the capture phase so xterm stops following new output before
          // its normal wheel handler processes the scroll delta.
          function handleWheelStart(event) {{
            const target = event.target;
            const insideTerminal = !target || target === window || target === document ||
              (target.closest && target.closest('.xterm'));
            if (insideTerminal && event.deltaY < 0) {{
              noteUserScrollIntent();
              _markUserScrolling();
              _scrollAwayFromBottom(event.deltaY);
            }}
          }}
          viewportEl.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});
          if (xtermEl) {{
            xtermEl.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});
          }}
          window.addEventListener('wheel', handleWheelStart, {{ capture: true, passive: true }});

          // 2. Track touch state on the viewport element
          var touchStartY = null;
          viewportEl.addEventListener('touchstart', function(event) {{
            _isTouchScrolling = true;
            touchStartY = null;
            if (event.touches && event.touches.length > 0) {{
              touchStartY = event.touches[0].clientY;
            }}
            if (_touchScrollTimer) {{
              clearTimeout(_touchScrollTimer);
              _touchScrollTimer = null;
            }}
          }}, {{ passive: true }});

          viewportEl.addEventListener('touchmove', function(event) {{
            if (touchStartY !== null && event.touches && event.touches.length > 0) {{
              if (event.touches[0].clientY - touchStartY > 8) {{
    noteUserScrollIntent();
    _markUserScrolling();
              }}
            }}
          }}, {{ passive: true }});

          viewportEl.addEventListener('touchend', function() {{
            _touchScrollTimer = setTimeout(function() {{
              _isTouchScrolling = false;
              _touchScrollTimer = null;
            }}, 500);
          }}, {{ passive: true }});

          return true;
        }}

"""
