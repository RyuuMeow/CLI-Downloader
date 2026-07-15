class CLIDownloaderError(Exception):
    """Base exception for CLI-Downloader."""


class ContractValidationError(CLIDownloaderError):
    """Raised when a public core contract contains invalid data."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")

    def to_dict(self) -> dict[str, str]:
        return {
            "error_type": type(self).__name__,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


class AdapterResolutionError(CLIDownloaderError):
    """Raised when a site adapter cannot resolve an item."""


class ConfigValidationError(CLIDownloaderError):
    """Raised when config values fail schema validation."""


class PluginValidationError(CLIDownloaderError):
    """Raised when plugin package metadata or manifest data is invalid."""


class DuplicateSiteAdapterError(CLIDownloaderError):
    """Raised when two adapters register the same site id."""


class TaskNotFoundError(CLIDownloaderError):
    """Raised when a task id cannot be found in the task store."""


class InvalidTaskTransitionError(CLIDownloaderError):
    """Raised when a task status cannot transition through the requested action."""


class TaskExecutionConflictError(InvalidTaskTransitionError):
    """Raised when a task already has an active execution owner."""


class StaleTaskRevisionError(InvalidTaskTransitionError):
    """Raised when a command or worker callback uses an old Task revision."""


class BatchNotFoundError(CLIDownloaderError):
    """Raised when persisted batch work cannot be found."""


class BatchConflictError(CLIDownloaderError):
    """Raised when persisted batch identities or relationships conflict."""


class OwnerError(CLIDownloaderError):
    """Base error for cross-process Scheduler-owner coordination."""

    code = "owner_error"


class OwnerAlreadyRunningError(OwnerError):
    """Raised when another process already owns the same data-root scope."""

    code = "owner_busy"


class OwnerLockError(OwnerError):
    """Raised when the owner lock cannot be created or acquired."""

    code = "owner_lock_error"


class OwnerDiscoveryError(OwnerError):
    """Raised when owner discovery metadata is malformed or cannot be persisted."""

    code = "owner_metadata_error"


class OwnerScopeMismatchError(OwnerError):
    """Raised when metadata does not belong to the current owner scope."""

    code = "owner_scope_mismatch"
