"""Exception snapshots for retained Runs, without execution or waiter frames."""

from __future__ import annotations

import copy
from types import MemberDescriptorType


def detached_error(error: BaseException) -> BaseException:
    """Copy exception semantics and chains without retaining any traceback.

    Raising a snapshot would attach the waiter's frames to it, so each waiter
    receives another detached copy. Original errors remain intact for logging.
    """
    try:
        return _detached_error(error)
    except BaseException:
        # Snapshotting is terminal bookkeeping: even an exception with broken
        # copy/accessor hooks must not keep a Run running or strand its waiters.
        return _unclonable_error(error)


def _unclonable_error(error: BaseException) -> RuntimeError:
    try:
        message = str(error)
    except BaseException:
        message = "exception details unavailable"
    return RuntimeError(f"{type(error).__name__}: {message}")


def _detached_error(error: BaseException) -> BaseException:
    copies: dict[int, BaseException] = {}
    pending: list[tuple[BaseException, BaseException]] = []

    def clone(source: BaseException) -> BaseException:
        if id(source) in copies:
            return copies[id(source)]
        try:
            result = allocate(source)
        except BaseException:
            result = _unclonable_error(source)
        copies[id(source)] = result
        pending.append((source, result))
        return result

    def allocate(source: BaseException) -> BaseException:
        result: BaseException
        if isinstance(source, BaseExceptionGroup):
            result = BaseExceptionGroup.__new__(
                type(source), source.message, [clone(child) for child in source.exceptions]
            )
        else:
            # Use the native exception allocator/state without invoking custom
            # constructors, which can require keywords or rewrite the message.
            native = next(
                base
                for base in type(source).__mro__
                if base.__module__ == "builtins" and issubclass(base, BaseException)
            )
            arguments = native.__reduce__(source)[1]
            try:
                result = native.__new__(type(source), *arguments)
                native.__init__(result, *arguments)
            except TypeError:
                # Native extension exceptions (for example Pydantic validation
                # errors) own their allocation and expose reconstruction via
                # the copy/reduction protocol instead of the built-in base.
                result = copy.copy(source)
                if result is source or type(result) is not type(source):
                    raise TypeError("Exception copy did not preserve independent state") from None
            result.args = source.args
        result.__dict__.update(source.__dict__)
        for base in type(source).__mro__:
            for name, attribute in vars(base).items():
                if name in {"message", "exceptions"} and base is BaseExceptionGroup:
                    continue
                if isinstance(attribute, MemberDescriptorType):
                    try:
                        value = attribute.__get__(source)
                    except AttributeError:
                        continue
                    if not hasattr(result, name) or value is not getattr(result, name):
                        attribute.__set__(result, value)
        if hasattr(source, "__notes__"):
            result.__notes__ = list(source.__notes__)
        result.__traceback__ = None
        return result

    snapshot = clone(error)
    while pending:
        source, result = pending.pop()
        result.__cause__ = clone(source.__cause__) if source.__cause__ is not None else None
        result.__context__ = clone(source.__context__) if source.__context__ is not None else None
        result.__suppress_context__ = source.__suppress_context__
    return snapshot
