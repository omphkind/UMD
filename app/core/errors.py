"""User-facing errors; ordinary failures never need a traceback."""


class UmdError(Exception):
    """A recoverable application error with a useful message."""


class ConfigurationError(UmdError):
    pass


class StorageError(UmdError):
    pass


class TaskBusyError(StorageError):
    pass


class SkipItem(UmdError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class MediaError(UmdError):
    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)
