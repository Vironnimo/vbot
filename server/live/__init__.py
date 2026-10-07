"""Server ownership of Live voice calls.

``registry`` owns the calls and ``owner`` defines the owner socket protocol;
``_call`` is the host one call runs on. The other private modules hold the
voice operator: how Live Tool calls are prepared (``_arguments``) and run
(``_tools`` dispatches to ``_sessions``, ``_terminals`` and
``_terminal_layout``, on ``_targets``, ``_programs`` and ``_context``), and Run
announcements (``_feed``). The Live Tool definitions are in
:mod:`core.tools.live`.
"""
