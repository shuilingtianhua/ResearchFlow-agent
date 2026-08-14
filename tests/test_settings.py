import pytest

from researchflow.settings import Settings


def test_settings_load_defaults_without_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "RESEARCHFLOW_APP_NAME",
        "RESEARCHFLOW_ENV",
        "RESEARCHFLOW_HOST",
        "RESEARCHFLOW_PORT",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    assert Settings.from_env() == Settings()
