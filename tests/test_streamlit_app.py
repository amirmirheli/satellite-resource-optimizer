"""Headless smoke test for the Streamlit app.

Skipped unless the optional UI dependency is installed (``uv sync --group ui``), so the default
test/CI path is unaffected. Uses Streamlit's AppTest to execute the script in a simulated runtime
and assert it runs end-to-end without raising.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


def test_app_runs_with_defaults() -> None:
    app = AppTest.from_file("streamlit_app.py", default_timeout=30)
    app.run()
    assert not app.exception


def test_app_runs_a_scenario() -> None:
    app = AppTest.from_file("streamlit_app.py", default_timeout=30)
    app.run()
    # Switch the base-scenario selectbox to a named scenario and re-run.
    app.selectbox[0].select("emergency_surge").run()
    assert not app.exception
    # The metrics row renders (served metric present).
    assert any("Served" in m.label for m in app.metric)
