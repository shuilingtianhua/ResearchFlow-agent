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
        "RESEARCHFLOW_DATABASE_URL",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    assert Settings.from_env() == Settings()


def test_settings_load_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCHFLOW_DATABASE_URL", "sqlite+aiosqlite:///custom.db")

    assert Settings.from_env().database_url == "sqlite+aiosqlite:///custom.db"
