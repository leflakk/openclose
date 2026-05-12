"""Built-in tools."""

from openclose.tool.tools.read import make_read_tool
from openclose.tool.tools.write import make_write_tool
from openclose.tool.tools.edit import make_edit_tool
from openclose.tool.tools.glob import make_glob_tool
from openclose.tool.tools.grep import make_grep_tool
from openclose.tool.tools.bash import make_bash_tool
from openclose.tool.tools.webfetch import make_webfetch_tool
from openclose.tool.tools.plan import make_plan_tool
from openclose.tool.tools.ask_user import make_ask_user_tool
from openclose.tool.tools.multiedit import make_multiedit_tool
from openclose.tool.tools.delegate import make_delegate_tool
from openclose.tool.tools.browser_automation import make_browser_automation_tool
from openclose.tool.tools.deliver_message import make_deliver_message_tool
from openclose.tool.registry import ToolRegistry


def register_all_tools(registry: ToolRegistry, project_dir: str = ".") -> None:
    """Register all built-in tools."""
    registry.register(make_read_tool(project_dir))
    registry.register(make_write_tool(project_dir))
    registry.register(make_edit_tool(project_dir))
    registry.register(make_glob_tool(project_dir))
    registry.register(make_grep_tool(project_dir))
    registry.register(make_bash_tool(project_dir))
    registry.register(make_webfetch_tool())
    registry.register(make_ask_user_tool())
    registry.register(make_multiedit_tool(project_dir))
    registry.register(make_browser_automation_tool(project_dir))
    registry.register(make_deliver_message_tool())
    # Register sub-agent tools last — they need a reference to the registry
    registry.register(make_plan_tool(project_dir, registry))
    registry.register(make_delegate_tool(project_dir, registry))
