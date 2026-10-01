"""Server ownership of Live voice calls.

``registry`` owns the calls and ``owner`` defines the owner socket protocol;
``_call`` is the host one call runs on. The other private modules hold the
voice operator: what the Models are told and the Live Tools (``_brief``), how
Model calls are prepared (``_arguments``) and run (``_tools`` dispatches to
``_sessions``, ``_terminals`` and ``_terminal_layout``, on ``_targets``,
``_programs`` and ``_context``), Run announcements (``_feed``), what it keeps
across calls (``_memory``), and Debug Mode records (``_record``).
"""
