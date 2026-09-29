import os

from dotenv import load_dotenv


def test_a_local_dotenv_file_cannot_leak_into_the_suite(tmp_path):
    # Guards tests/conftest.py's PYTHON_DOTENV_DISABLED: if that line is
    # ever removed, a developer's real .env would again silently supply
    # API keys to the commands under test and hide failures that CI (which
    # has no .env) would hit.
    env_file = tmp_path / ".env"
    env_file.write_text("KUKISTI_ENV_ISOLATION_SENTINEL=leaked\n")
    try:
        loaded = load_dotenv(env_file)
        assert loaded is False
        assert "KUKISTI_ENV_ISOLATION_SENTINEL" not in os.environ
    finally:
        os.environ.pop("KUKISTI_ENV_ISOLATION_SENTINEL", None)
