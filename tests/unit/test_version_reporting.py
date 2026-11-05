"""The version reported to stages and probes is the version that ran."""

from ci_fabric import __version__, context, deployment


def test_stage_environment_reports_the_running_version() -> None:
    assert getattr(context, "LOCALCI_VERSION", None) == __version__


def test_deployment_user_agent_reports_the_running_version() -> None:
    expected = f"ci-fabric/{__version__}"
    assert expected == deployment.USER_AGENT
