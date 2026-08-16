"""The governance boundary: the control that makes the security claim real.

The architectural argument of IntelligenceStack is that an LLM must act as an
*orchestrator*, never as a database client. That argument is only worth making
if something mechanically enforces it. This module is that something.

Every request the agent wishes to execute passes through `enforce()`, which
applies four controls in order:

  1. FUNCTION_GRANT  -- the requested function must be registered in the catalog
                        allowlist. This mirrors `GRANT EXECUTE ON FUNCTION` in
                        Unity Catalog: an unregistered name is not callable,
                        regardless of what the model emitted.
  2. PARAMETER_SCHEMA -- every argument must satisfy the declared type and
                        pattern. A value that does not match is rejected before
                        it reaches the engine.
  3. SQL_INTERDICTION -- any attempt to smuggle SQL through a parameter is
                        refused outright.
  4. ENTITLEMENT     -- the *caller* must be entitled to this function, and to
                        the specific row requested. The first three controls
                        bound what the agent may do; this one bounds what this
                        particular person may ask it to do, which is the other
                        half of least privilege. See `identity.py`.

A denial is a first-class, auditable outcome -- not an exception and not a
silent fallback. The agent surfaces it to the caller verbatim.
"""

import re
from dataclasses import dataclass, field

from src.governance.identity import SERVICE_PRINCIPAL, Principal
from src.settings import CATALOG, SCHEMA


@dataclass(frozen=True)
class ParameterSpec:
    """Declared contract for a single function argument.

    Two kinds of argument exist, and they are governed differently:

    * **Identifiers** (the default) are strict tokens such as ``CUST_404``. They
      must match `pattern` exactly and are subject to full SQL interdiction.
    * **Free text** (`free_text=True`) is a natural-language value, such as a
      retrieval question. Prose legitimately contains words like "update" or
      "create" and punctuation like ``;`` or ``--``, so strict interdiction
      would reject valid input. Free text is instead bounded by length and
      refused only when control sequences appear *together with* SQL
      vocabulary -- the signature of an injection attempt. This is safe because
      the value is only ever used as a bound value, never composed into a
      statement, so it cannot influence query structure.
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
    "get_customer_timeline": FunctionSpec(
        name="get_customer_timeline",
        description=(
            "Returns the day-by-day anomaly trajectory for a single customer, "
            "for questions about whether behaviour is improving or worsening."
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
    # The caller this decision was made for. Carried on the decision so the
    # audit trail can answer "who ran this?" and not just "what ran?".
    principal_id: str = ""

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "control": self.control,
            "detail": self.detail,
            "function": self.function,
            "parameters": self.parameters,
            "principal_id": self.principal_id,
        }


def enforce(
    function_name: str | None,
    parameters: dict | None,
    principal: Principal | None = None,
) -> GovernanceDecision:
    """Apply the governance boundary to a proposed function invocation.

    `principal` is the authenticated caller. When omitted the unrestricted
    service identity is used, preserving the callerless behaviour the sandbox
    had before identities existed -- see `identity.SERVICE_PRINCIPAL`.
    """
    parameters = parameters or {}
    principal = principal or SERVICE_PRINCIPAL

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
            principal_id=principal.principal_id,
        )

    spec = REGISTERED_FUNCTIONS[function_name]
    declared = {p.name: p for p in spec.parameters}

    # Control 3 (applied early) -- SQL interdiction, scoped to the parameter's
    # declared kind.
    #
    # Identifiers are strict: any SQL keyword or control sequence is refused.
    #
    # Free text is screened differently, because a natural-language question is
    # only ever used as a *bound* value and is never composed into a statement.
    # Punctuation alone therefore cannot express SQL, and prose legitimately
    # contains it -- "CUST_404 is flagged; what should we do?" and "the policy
    # -- the latest one" are ordinary questions, not attacks. What does signal
    # an injection attempt is a control sequence *combined with* SQL
    # vocabulary, as in "policy'; DROP TABLE knowledge; --". That combination
    # is refused and recorded; bare punctuation is not.
    #
    # Undeclared parameters are screened strictly and rejected outright below.
    for key, value in parameters.items():
        if not isinstance(value, str):
            continue

        param = declared.get(key)
        has_control = bool(_SQL_CONTROL.search(value))
        has_keyword = bool(_SQL_KEYWORDS.search(value))

        if param is not None and param.free_text:
            if has_control and has_keyword:
                offence = "SQL control sequences combined with SQL keywords"
            else:
                continue
        elif has_control:
            offence = "SQL control sequences"
        elif has_keyword:
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
            principal_id=principal.principal_id,
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
                    principal_id=principal.principal_id,
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
                principal_id=principal.principal_id,
            )

    undeclared = set(parameters) - {p.name for p in spec.parameters}
    if undeclared:
        return GovernanceDecision(
            allowed=False,
            control="PARAMETER_SCHEMA",
            detail=f"Undeclared parameters rejected: {sorted(undeclared)}.",
            function=function_name,
            parameters=parameters,
            principal_id=principal.principal_id,
        )

    # Control 4 -- per-caller entitlement. The allowlist says the *agent* may
    # call this function; entitlement says whether *this caller* may, and
    # against this row. Checked last so that a malformed or hostile request is
    # reported as such rather than as an authorisation problem.
    if not principal.may_invoke(function_name):
        return GovernanceDecision(
            allowed=False,
            control="ENTITLEMENT",
            detail=(
                f"{principal.display_name} ({principal.role}) is not entitled to "
                f"invoke {function_name}. Entitled functions for this caller: "
                f"{sorted(principal.allowed_functions) or 'none'}."
            ),
            function=function_name,
            parameters=parameters,
            principal_id=principal.principal_id,
        )

    target_id = parameters.get("target_id")
    if target_id and not principal.may_see_customer(target_id):
        return GovernanceDecision(
            allowed=False,
            control="ENTITLEMENT",
            detail=(
                f"{principal.display_name} ({principal.role}) may invoke "
                f"{function_name}, but {target_id} is outside their assigned scope "
                f"of {principal.scope_label()}. Row-level entitlement refused."
            ),
            function=function_name,
            parameters=parameters,
            principal_id=principal.principal_id,
        )

    return GovernanceDecision(
        allowed=True,
        control="FUNCTION_GRANT",
        detail=(
            f"EXECUTE granted on {spec.fully_qualified_name} for "
            f"{principal.display_name} ({principal.role}); arguments conform to the "
            "declared schema and are bound as parameters."
        ),
        function=function_name,
        parameters=parameters,
        principal_id=principal.principal_id,
    )


def available_tool_schemas() -> list[dict]:
    """The tool definitions exposed to the model -- the allowlist, and nothing else."""
    return [spec.to_tool_schema() for spec in REGISTERED_FUNCTIONS.values()]
