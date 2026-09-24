"""One-off, best-effort conversion of a pre-Generation-1 data directory.

Each area module exposes ``convert(context: ConversionContext) -> None``. An area
reads the source data directory without modifying it, writes every new or
rewritten file under ``context.staging`` at the relative path it will have in
the data directory, and retires source files the Generation 1 layout no longer
uses. The package entry point runs the areas in order, verifies the staged
result and installs it.
"""
