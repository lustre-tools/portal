"""Stand-ins that let the graph tool run without Gerrit or the gc CLI."""


class FakeSocket:
    """Records emits instead of sending them over a WebSocket."""

    def __init__(self):
        self.events = []

    def emit(self, event, data=None, room=None, to=None):
        self.events.append((event, data or {}))

    def completes(self):
        return [d for (e, d) in self.events if e == "complete"]

    def output(self):
        return "".join(d.get("line", "") for (e, d) in self.events if e == "output")


class FakePopen:
    """Records the command and streams progress plus the CLI's final JSON.

    The trailing JSON line matters: the tool parses it to recover the
    resolved ticket anchor, which is an undocumented contract with the
    real gc CLI.
    """

    last_cmd = None

    def __init__(self, cmd, **kwargs):
        FakePopen.last_cmd = cmd
        self.returncode = 0
        self.stdout = iter(
            [
                "Building graph...\n",
                '{"anchor": 62796, "tickets": ["LU-19921"], "html_path": "/srv/x.html"}\n',
            ]
        )

    def wait(self):
        return 0
