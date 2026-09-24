"""Small filesystem helpers shared by the two on-disk stores."""

import os


def owner_to_keep(path):
    """The (uid, gid) a rewritten file should keep, or None.

    Both stores are replaced by writing a temp file and renaming it, and
    a temp file belongs to whoever created it. When that is root --
    which is exactly who runs ``portal-users`` or ``portal-refresh`` by
    hand on a server -- the replacement comes out root-owned and 0600,
    unreadable by the service user. For the account store that means
    every login fails; for the graph index, the whole site.

    So when running as root, keep the existing file's owner, or the
    directory's for a file being created. Anyone else cannot change
    ownership anyway, and needs nothing.
    """
    if os.geteuid() != 0:
        return None
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = os.stat(os.path.dirname(os.path.abspath(path)) or ".")
    return st.st_uid, st.st_gid
