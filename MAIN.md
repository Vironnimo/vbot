- Python fuer backend
- Svelte + JS fuer frontend
- LOGS!
- model database ueber openrouter und models.dev bauen, aber nur wichtige parameter! vielleicht die model database nur ueber ein script bauen? also quasi ausserhalb des app codes.
- "Async-first Runtime
Der Kernel ist rein Threading-basiert mit manuellem Lock-Management. Für ein System, das gleichzeitig Streaming-Chat, WebSocket-Push, parallele Subagents, Voice, Telegram-Polling und Cron jongliert, wäre asyncio die natürlichere Wahl. uvicorn läuft bereits async — der Rest des Kernels tut es nicht. Das führt zu run_in_threadpool-Workarounds und komplexen threading.Event-Konstrukten, die asyncio durch asyncio.create_task() + CancelledError ersetzen würde."
- localization