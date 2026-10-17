"""Static CSS injected into proxied ttyd pages."""

TERMINAL_STYLE = """
    <style>
      /* Improve layout and touch scrolling behavior */
      html, body {
        overscroll-behavior: none;
        -webkit-overflow-scrolling: touch;
        /* Use full viewport height so keyboard doesn't shrink the layout */
        height: 100lvh;
      }
      body {
        margin: 0;
        padding: 0;
        overflow: hidden;
      }
      /* Mobile touch scrolling with native inertia.
         xterm.js layers (bottom to top): .xterm-viewport (scrollable),
         .xterm-screen, canvas (render), canvas.xterm-link-layer.
         Touch events land on the topmost canvas, never reaching
         .xterm-viewport, so browser-native inertial scroll never fires.
         Fix: let touches pass through .xterm-screen to .xterm-viewport.

         IMPORTANT: scope this to coarse (touch) pointers only. On desktop
         (fine pointer / mouse) the passthrough is unnecessary and semantically
         wrong — it routes mouse events to the viewport instead of the screen —
         so keep .xterm-screen interactive there for normal drag-selection.
         On touch devices, drag-to-select is handled explicitly via the
         "select text" mode (see the select-mode handler below), so we can keep
         the passthrough for smooth inertial scrolling by default. */
      @media (pointer: coarse) {
        .xterm-screen {
          pointer-events: none !important;
        }
        /* When the user turns on the explicit select-text mode we flip this
           attribute on <html> so touches reach the screen for selection. */
        html[data-select-mode='on'] .xterm-screen {
          pointer-events: auto !important;
        }
      }
      .xterm-viewport {
        -webkit-overflow-scrolling: touch !important;
        /* Slim, theme-matching overlay scrollbar (Firefox).
           The default thick scrollbar sits flush against the iframe edge and
           its bottom gets clipped by the host pane's rounded corner, which
           looks awkward. A thin translucent bar blends with the terminal. */
        scrollbar-width: thin !important;
        scrollbar-color: rgba(255, 255, 255, 0.28) transparent !important;
      }
      /* WebKit/Blink slim overlay scrollbar for the xterm viewport. */
      .xterm-viewport::-webkit-scrollbar {
        width: 8px;
        height: 8px;
        background: transparent;
      }
      .xterm-viewport::-webkit-scrollbar-track {
        background: transparent;
      }
      .xterm-viewport::-webkit-scrollbar-thumb {
        background-color: rgba(255, 255, 255, 0.22);
        border-radius: 8px;
        /* Inset the thumb from the edges so it never touches the rounded
           pane corner, and shrink it via a transparent border box. */
        border: 2px solid transparent;
        background-clip: padding-box;
      }
      .xterm-viewport::-webkit-scrollbar-thumb:hover {
        background-color: rgba(255, 255, 255, 0.4);
      }
      .xterm-viewport::-webkit-scrollbar-corner {
        background: transparent;
      }
    </style>
"""
