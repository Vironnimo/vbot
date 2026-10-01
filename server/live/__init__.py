"""Server ownership of Live voice calls.

``registry`` owns the calls, their owner sockets and UI requests; the private
modules hold the voice operator: what the Models are told and the Live Tools
(``_brief``), how Model calls are prepared (``_arguments``) and run
(``_tools``, ``_terminals``, ``_programs``, ``_targets``, ``_context``), Run
announcements (``_feed``), and Debug Mode records (``_record``).
"""
