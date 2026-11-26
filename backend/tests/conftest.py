"""Tests never read the developer .env: results must not depend on whose machine runs them."""
import os

os.environ["EDUSYNC_NO_DOTENV"] = "1"

import pytest


@pytest.fixture(autouse=True)
def no_embedding_models(monkeypatch, request):
    """Matching tests use the shared-words fallback: no model download, no network, same result everywhere.
    A test marked @pytest.mark.real_embeddings uses the real local model."""
    if request.node.get_closest_marker("real_embeddings"):
        return
    from app import llm, video

    def off(*a, **k):
        raise RuntimeError("embeddings disabled in tests")

    def off_hosted(*a, **k):
        raise llm.LLMError("embeddings disabled in tests")

    monkeypatch.setattr(video, "local_embed", off)
    monkeypatch.setattr(llm, "embed", off_hosted)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_embeddings: run with the real local sentence model (downloads it once)")
