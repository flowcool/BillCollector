# Same-origin noVNC routing

Set `BC_NOVNC_PATH=/browser` to serve the embedded browser through an HTTPS
reverse proxy on the UI origin. The generated iframe uses
`/browser/vnc.html?autoconnect=true&path=browser%2Fwebsockify`. The proxy must
strip `/browser` and forward HTTP and WebSocket traffic to port 6080.

Set `display_count=1` and `max_concurrency=1` in Settings for this fixed backend.
Protect the browser route with the same authentication as the UI. The daemon's
session middleware does not protect the separate websockify process.
Do not publish websockify or raw VNC ports on the host.

When `BC_NOVNC_PATH` is absent, the existing direct-port URL stays unchanged.
Only absolute path prefixes are accepted. RFB passwords stay out of the URL.

Verification: `python -m pytest tests/test_bc_novnc_proxy.py -q`.
These checks run generated JavaScript in Chromium with synthetic page responses.
