"""Error taxonomy shared by domain and application modules."""


class ResearchFlowError(Exception):
    """Base class for expected ResearchFlow failures."""


class ContractViolation(ResearchFlowError):
    """A command, plan, or state transition violates a contract."""


class ConflictError(ResearchFlowError):
    """Concurrent state or execution lease no longer matches."""


class NotFoundError(ResearchFlowError):
    """A requested domain object does not exist."""


class PolicyViolation(ResearchFlowError):
    """An operation violates a budget, sandbox, or research policy."""


class DependencyUnavailable(ResearchFlowError):
    """An external dependency is temporarily unavailable."""


class ExecutionFailure(ResearchFlowError):
    """A real task or experiment execution failed."""
