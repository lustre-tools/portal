"""Run the portal.

    portal                      # or: python -m portal

Binds loopback by default: the expected deployment is behind a reverse
proxy that terminates TLS. Set PORTAL_BIND_HOST=0.0.0.0 to expose it
directly, and understand what that means for the session cookie.
"""

# Must run before anything else imports ssl, socket or threading --
# eventlet patches those modules in place, and a module that grabbed the
# unpatched version first will block the whole server on its first wait.
import eventlet

eventlet.monkey_patch()

import os  # noqa: E402


def main():
    from portal.app import create_app, socketio

    app = create_app()
    host = os.environ.get("PORTAL_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("PORTAL_BIND_PORT", "5000"))
    socketio.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
