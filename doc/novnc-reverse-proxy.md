# Same-origin noVNC routing

Set `BC_NOVNC_PATH=/browser` to serve the embedded browser through an HTTPS
reverse proxy on the UI origin. The generated iframe uses
`/browser/vnc.html?autoconnect=true&path=browser%2Fwebsockify`. The proxy must
strip `/browser` and forward HTTP and WebSocket traffic to port 6080.

Set `display_count=1` and `max_concurrency=1` in Settings for this fixed backend.
For a proxy in another container, explicitly set `BC_NOVNC_HOST=0.0.0.0` on a
trusted private network. The default passwordless listener is loopback-only.
The daemon checks the effective stored/default settings before starting tasks
and rejects conflicting edits. Environment defaults alone do not override saved
settings. See the [complete container guide](container-daemon.md) and its Compose
overrides for persistence, UID/GID, bootstrap and upgrade instructions.
Protect the browser route with the same authentication as the UI. The daemon's
session middleware does not protect the separate websockify process.
Do not publish websockify or raw VNC ports on the host.

When `BC_NOVNC_PATH` is absent, the existing direct-port URL stays unchanged.
Only absolute path prefixes are accepted. RFB passwords stay out of the URL.

Verification: `python -m pytest tests/test_bc_novnc_proxy.py -q`.
These checks run generated JavaScript in Chromium with synthetic page responses.
After building the image, `python tests/check_container_compose.py --image IMAGE`
also checks a real browser frame and RFB WebSocket through a separate authenticated
HTTPS proxy. An anonymous redirect alone is not that integration proof.
