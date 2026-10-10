import pytest


@pytest.fixture
def anyio_backend():
    """
    Run @pytest.mark.anyio tests on asyncio.

    anyio ships its own pytest plugin, so async tests need no extra
    dependency — this fixture just pins the backend so they don't also run
    under trio (which isn't installed).
    """
    return "asyncio"
