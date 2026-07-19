"""The governance boundary: the control that makes the security claim real.

The architectural argument of IntelligenceStack is that an LLM must act as an
*orchestrator*, never as a database client. That argument is only worth making
if something mechanically enforces it. This module is that something.

Every request the agent wishes to execute passes through `enforce()`, which
applies three controls in order:

  1. FUNCTION_GRANT  -- the requested function must be registered in the catalog
                        allowlist. This mirrors `GRANT EXECUTE ON FUNCTION` in
                        Unity Catalog: an unregistered name is not callable,
                        regardless of what the model emitted.
  2. PARAMETER_SCHEMA -- every argument must satisfy the declared type and
                        pattern. A value that does not match is rejected before
                        it reaches the engine.
  3. SQL_INTERDICTION -- any attempt to smuggle SQL through a parameter is
                        refused outright.

A denial is a first-class, auditable outcome -- not an exception and not a
silent fallback. The agent surfaces it to the caller verbatim.
"""

import re
from dataclasses import dataclass, field

from src.settings import CATALOG, SCHEMA


@dataclass(frozen=True)
class ParameterSpec:
    """Declared contract for a single function argument.

    Two kinds of argument exist, and they are governed differently:

    * **Identifiers** (the default) are strict tokens such as ``CUST_404``. They
      must match `pattern` exactly and are subject to full SQL interdiction.
    * **Free text** (`free_text=True`) is a natural-language value, such as a
      retrieval question. A question legitimately contains words like "update"
      or "create", so keyword-level SQL interdiction would reject valid input.
      Free text is instead bounded by length and screened for SQL *control
      sequences*; it is only ever used as a bound value (never composed into a
      statement), so it cannot influence query structure.
    """

    name: str
    pattern: str
    description: str
    required: bool = True
    free_text: bool = False
    max_length: int = 500

    def validate(self, value) -> str | None:
        """Return an error string if `value` violates the contract."""
        if not isinstance(value, str):
            return f"parameter '{self.name}' must be a string, received {type(value).__name__}"

        if self.free_text:
            if not value.strip():
                return f"parameter '{self.name}' must not be empty"
            if len(value) > self.max_length:
                return (
                    f"parameter '{self.name}' exceeds the declared maximum length of "
                    f"{self.max_length} characters"
                )
            return None

        if not re.fullmatch(self.pattern, value):
            return (
                f"parameter '{self.name}' value {value!r} does not satisfy the "
                f"declared pattern {self.pattern!r}"
            )
        return None


@dataclass(frozen=True)
class FunctionSpec:
    """A Unity Catalog function the agent has been granted EXECUTE on."""

    name: str
    description: str
    parameters: list[ParameterSpec] = field(default_factory=list)

    @property
    def fully_qualified_name(self) -> str:
        return f"{CATALOG}.{SCHEMA}.{self.name}"

    def to_tool_schema(self) -> dict:
        """Render as a tool definition for the model's instruction set."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    p.name: {"type": "string", "description": p.description}
                    for p in self.parameters
                },
                "required": [p.name for p in self.parameters if p.required],
            },
        }


# The catalog allowlist. Adding a capability to the agent is a deliberate,
# reviewable act of registering it here -- it cannot acquire one at runtime.
REGISTERED_FUNCTIONS: dict[str, FunctionSpec] = {
    "get_customer_anomaly_score": FunctionSpec(
        name="get_customer_anomaly_score",
        description=(
            "Calculates statistical anomaly counts and behavioural risk for a "
            "single customer identifier."
        ),
        parameters=[
            ParameterSpec(
                name="target_id",
                pattern=r"CUST_\d{3}",
                description="Customer identifier in the form CUST_123.",
            )
        ],
    ),
    "search_knowledge_base": FunctionSpec(
        name="search_knowledge_base",
        description=(
            "Retrieves relevant passages from the governed enterprise document "
            "corpus (runbooks, playbooks, and policies) for a natural-language "
            "question. Returns cited excerpts, never raw source files."
        ),
        parameters=[
            ParameterSpec(
                name="query",
                pattern=r".+",
                description="Natural-language question to retrieve governed passages for.",
                free_text=True,
            )
        ],
    ),
}

# Keywords that indicate an attempt to express SQL rather than supply a value.
# Applied to identifier parameters only -- a natural-language question may
# legitimately contain words like "update" or "create".
_SQL_KEYWORDS = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"union|exec|execute)\b",
    re.IGNORECASE,
)

# Control sequences used to terminate or comment out a statement. These have no
# legitimate place in *any* parameter value and are rejected everywhere.
_SQL_CONTROL = re.compile(r"--|;|/\*")


@dataclass(frozen=True)
class GovernanceDecision:
    """The auditable outcome of a boundary check."""

    allowed: bool
    control: str
    detail: str
    function: str | None = None
    parameters: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "control": self.control,
            "detail": self.detail,
            "function": self.function,
            "parameters": self.parameters,
        }


def enforce(function_name: str | None, parameters: dict | None) -> GovernanceDecision:
    """Apply the governance boundary to a proposed function invocation."""
    parameters = parameters or {}

    # Control 1 -- function-level grant.
    if not function_name or function_name not in REGISTERED_FUNCTIONS:
        return GovernanceDecision(
            allowed=False,
            control="FUNCTION_GRANT",
            detail=(
                f"No EXECUTE grant for {function_name!r}. The agent may only invoke "
                f"functions registered in {CATALOG}.{SCHEMA}: "
                f"{sorted(REGISTERED_FUNCTIONS)}."
            ),
            function=function_name,
            parameters=parameters,
        )

    spec = REGISTERED_FUNCTIONS[function_name]
    declared = {p.name: p for p in spec.parameters}

    # Control 3 (applied early) -- SQL interdiction, scoped to the parameter's
    # declared kind. Control sequences are rejected in every value; the keyword
    # filter applies only to identifiers, since free-text questions may
    # legitimately contain words such as "update" or "create". Undeclared
    # parameters are screened strictly and rejected outright below.
    for key, value in parameters.items():
        if not isinstance(value, str):
            continue

        param = declared.get(key)
        if _SQL_CONTROL.search(value):
            offence = "SQL control sequences"
        elif (param is None or not param.free_text) and _SQL_KEYWORDS.search(value):
            offence = "SQL keywords"
        else:
            continue

        return GovernanceDecision(
            allowed=False,
            control="SQL_INTERDICTION",
            detail=(
                f"Parameter '{key}' contains {offence}. The agent is not permitted "
                "to express SQL; it may only supply values to pre-approved functions."
            ),
            function=function_name,
            parameters=parameters,
        )

    # Control 2 -- parameter schema conformance.
    for param in spec.parameters:
        if param.name not in parameters:
            if param.required:
                return GovernanceDecision(
                    allowed=False,
                    control="PARAMETER_SCHEMA",
                    detail=f"Required parameter '{param.name}' was not supplied.",
                    function=function_name,
                    parameters=parameters,
                )
            continue

        error = param.validate(parameters[param.name])
        if error:
            return GovernanceDecision(
                allowed=False,
                control="PARAMETER_SCHEMA",
                detail=error,
                function=function_name,
                parameters=parameters,
            )

    undeclared = set(parameters) - {p.name for p in spec.parameters}
    if undeclared:
        return GovernanceDecision(
            allowed=False,
            control="PARAMETER_SCHEMA",
            detail=f"Undeclared parameters rejected: {sorted(undeclared)}.",
            function=function_name,
            parameters=parameters,
        )

    return GovernanceDecision(
        allowed=True,
        control="FUNCTION_GRANT",
        detail=(
            f"EXECUTE granted on {spec.fully_qualified_name}; arguments conform to "
            "the declared schema and are bound as parameters."
        ),
        function=function_name,
        parameters=parameters,
    )


def available_tool_schemas() -> list[dict]:
    """The tool definitions exposed to the model -- the allowlist, and nothing else."""
    return [spec.to_tool_schema() for spec in REGISTERED_FUNCTIONS.values()]
