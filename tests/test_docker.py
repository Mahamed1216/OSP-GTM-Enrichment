"""Docker packaging guards: the image manifest exists and never ships secrets."""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_dockerfile_exists_and_has_entrypoint():
    df = _ROOT / "Dockerfile"
    assert df.exists(), "Dockerfile must exist at the repo root"
    text = df.read_text(encoding="utf-8")
    # Installs deps and launches the real Streamlit entrypoint by default.
    assert "requirements.txt" in text
    assert "requirements-ui.txt" in text
    assert "app/main.py" in text  # the actual UI entrypoint (not app/Home.py)
    assert "8501" in text


def test_dockerignore_excludes_secrets_and_local_artifacts():
    di = _ROOT / ".dockerignore"
    assert di.exists(), ".dockerignore must exist"
    text = di.read_text(encoding="utf-8")
    for needle in (
        ".env",
        ".streamlit/secrets.toml",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "logs",
        "*.db",
        "*.sqlite",
    ):
        assert needle in text, f".dockerignore must exclude {needle}"


def test_compose_exists_with_api_and_scheduler():
    dc = _ROOT / "docker-compose.yml"
    assert dc.exists()
    text = dc.read_text(encoding="utf-8")
    assert "src.api.server:app" in text       # API service
    assert "src.lead_source.scheduler" in text  # scheduler service
    assert "env_file" in text                 # local-only env, not baked in
    # SalesOS shared-Supabase integration workers (primary production model).
    assert "src.integrations.salesos.worker" in text
    assert "src.integrations.salesos.send_approved" in text
