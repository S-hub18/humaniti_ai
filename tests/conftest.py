import pytest


@pytest.fixture(autouse=True)
def _unlimited_gemini_pacing(monkeypatch):
    """Tests use fake clients; the shared real-time token bucket must never sleep in them."""
    from cqa import gemini_retry

    monkeypatch.setenv("GEMINI_TPM", "1000000000000")
    gemini_retry.BUCKET.events.clear()
    yield
    gemini_retry.BUCKET.events.clear()
