"""Select the running server's local log directories, never a client-supplied path."""

from pathlib import Path

from cli.application.state import ApplicationError, contained, discover
from core.utils.log_viewer import LogViewer
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_WORKERS = BoundedWorkerPool(name="log-sources", max_workers=1)
_LOGGER = get_logger("server.logs")


async def build_log_viewer(data_dir: str | Path) -> LogViewer:
    return await _WORKERS.run(_build_log_viewer, Path(data_dir))


def _build_log_viewer(data_dir: Path) -> LogViewer:
    additional: dict[str, Path] = {}
    try:
        installation = discover()
        if (
            installation is not None
            and installation.owns_server
            and installation.server_data_directory
            and Path(installation.server_data_directory).resolve() == data_dir.resolve()
        ):
            additional["application"] = contained(installation.root, "logs")
    except (ApplicationError, OSError) as error:
        _LOGGER.warning("Application logs unavailable (error_type=%s)", type(error).__name__)
    return LogViewer(data_dir, additional_directories=additional)
