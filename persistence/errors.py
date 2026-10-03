"""Storage failures with messages that are safe to show to browsers and put in logs.

Driver exceptions can embed hosts, usernames or SQL; they are never chained into these errors.
"""


class StorageError(Exception):
    status = 503
    public_message = "The family database is temporarily unavailable. Please try again."

    def __init__(self, message=None):
        super().__init__(message or self.public_message)


class StorageConflict(StorageError):
    status = 409
    public_message = "That change conflicted with another update. Please reload and try again."


class StorageInvalid(StorageError):
    status = 400
    public_message = "That item contains data that cannot be stored."


class SchemaNotReady(StorageError):
    """The PostgreSQL database has not been migrated to the version this code requires."""
