"""Access security kernel: access modes, paired devices and the local access token.

``core.access.config`` validates the host's access configuration,
``core.access.devices`` owns the device store and device tokens, and
``core.access.local_token`` the per-start local access token. The server edge
that enforces them is ``server.access``.
"""
