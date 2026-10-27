"""Domain errors. The API turns them into 4xx responses with the message as `detail`."""


class IntelError(Exception):
    status_code = 422

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(IntelError):
    status_code = 404


class Conflict(IntelError):
    status_code = 409


class Forbidden(IntelError):
    status_code = 403
