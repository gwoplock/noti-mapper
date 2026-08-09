"""behave hooks: build a world before each scenario and take it apart after.

Every scenario gets its own scratch directory, its own database, and its own
PagerDuty stub, so nothing one scenario latches can be seen by the next. That
is what "in an isolated manner" has to mean for a daemon whose entire job is
remembering things across restarts.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

from tests.features.support import world as support


def before_all(context: Any) -> None:
    context.backend = support.backend_name()


def before_scenario(context: Any, scenario: Any) -> None:
    del scenario
    context.workspace = Path(tempfile.mkdtemp(prefix="noti-bdd-"))
    context.stub = support.start_stub()
    context.daemon = support.make_daemon(
        workspace=context.workspace, pagerduty_url=context.stub.base_url()
    )
    context.world = support.World(
        daemon=context.daemon, stub=context.stub, workspace=context.workspace
    )
    # A sensible default every scenario can override in its Background.
    context.rules = {"Package On Porch": {"inputs": ["Porch Hook"], "outputs": ["Porch Pager"]}}
    context.cli_result = None


def after_scenario(context: Any, scenario: Any) -> None:
    del scenario
    daemon = getattr(context, "daemon", None)
    if daemon is not None:
        try:
            daemon.stop()
        finally:
            tear_down = getattr(daemon, "tear_down", None)
            if tear_down is not None:
                tear_down()

    stub = getattr(context, "stub", None)
    if stub is not None:
        stub.stop()

    workspace = getattr(context, "workspace", None)
    if workspace is not None:
        _remove_workspace(workspace)


def _remove_workspace(workspace: Path) -> None:
    """Delete a scenario's scratch directory, having checked it is one.

    rmtree takes whatever it is given. The path always comes from mkdtemp, so
    this can never fire -- which is the point of asserting it rather than
    trusting that it stays true.
    """
    root = Path(tempfile.gettempdir()).resolve()
    resolved = workspace.resolve()
    if not resolved.is_relative_to(root) or not resolved.name.startswith("noti-bdd-"):
        raise AssertionError(f"refusing to delete {resolved}, which is not a scenario workspace")
    shutil.rmtree(resolved, ignore_errors=True)
