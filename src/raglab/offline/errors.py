from dataclasses import FrozenInstanceError, dataclass

from raglab.contracts.errors import _EXCEPTION_METADATA


@dataclass(slots=True, repr=False)
class OperatorError(Exception):
    """Stable operator guidance containing only approved, non-content values."""

    code: str
    stage: str
    summary: str
    observed: str
    expected: str
    protected_state: str
    retry_safe: bool
    next_command: str
    docs: str
    exit_code: int = 2

    def __setattr__(self, name: str, value: object) -> None:
        if name in _EXCEPTION_METADATA or (
            name in OperatorError.__slots__ and not hasattr(self, name)
        ):
            Exception.__setattr__(self, name, value)
            return
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        if name in _EXCEPTION_METADATA:
            Exception.__delattr__(self, name)
            return
        raise FrozenInstanceError(f"cannot delete field {name!r}")

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int or self.exit_code not in {2, 3, 4, 5, 6, 7, 10}:
            raise ValueError("unsupported operator exit code")
        Exception.__init__(self, self.code)
