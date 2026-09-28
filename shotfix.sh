#!/bin/bash
sed "s|infinite; }|infinite; animation-delay: -$1ms; }|" /tmp/sl_one_fix.html > /tmp/sl_one_fix_d.html
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --force-device-scale-factor=6 --screenshot="$2" --window-size=120,80 "file:///tmp/sl_one_fix_d.html" 2>/dev/null
