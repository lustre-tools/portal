from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ToolParam:
    name: str
    label: str
    type: str  # "text" or "checkbox"
    required: bool = False
    placeholder: str = ""
    help_text: str = ""
    # If True, the field is only rendered for sessions that can view
    # internal projects (the internal role). UI hiding is cosmetic — the value
    # is also dropped server-side for non-privileged sessions.
    internal_only: bool = False


@dataclass
class ToolDefinition:
    id: str
    name: str
    description: str
    parameters: list[ToolParam] = field(default_factory=list)
    run_fn: Callable = None
    output_type: str = "html_file"  # "html_file", "text", "json"
    output_dir: str | None = None
    public_output: bool = False  # If True, generated output is viewable without login


TOOLS: dict[str, ToolDefinition] = {}


def register_tool(tool: ToolDefinition):
    TOOLS[tool.id] = tool


def get_tool(tool_id: str) -> ToolDefinition | None:
    return TOOLS.get(tool_id)


def list_tools() -> list[ToolDefinition]:
    return list(TOOLS.values())
