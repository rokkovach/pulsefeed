"""Production bootstrap must not turn missing model credentials into fake jobs."""
import pytest
from fastapi.testclient import TestClient
from pulsefeed.config import Config
from pulsefeed.main import create_app


def config(tmp_path, **changes):
    values = dict(database_path=str(tmp_path / "production.db"),
                  base_url="https://pulsefeed.example", session_secret="s" * 48,
                  inference_enabled=False, fake_mode=False)
    values.update(changes)
    return Config(**values)


def test_paused_production_boots_without_provider_credentials_and_admits_nothing(tmp_path):
    def unexpected_fetch(_):
        raise AssertionError("Paused admission must not fetch a feed")

    app = create_app(config(tmp_path), feed_fetcher=unexpected_fetch)
    with TestClient(app, base_url="https://pulsefeed.example") as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "generation-paused"}
        home = client.get("/")
        assert home.status_code == 200
        assert "Image generation is paused" in home.text
        assert "synthetic examples" not in home.text
        for method, path in (("POST", "/api/view-feed"),
                             ("POST", "/api/runs/anything/retry"),
                             ("GET", "/api/runs/anything/image")):
            result = client.request(method, path, json={})
            assert result.status_code == 503
            assert result.json()["error"]["code"] == "INFERENCE_PAUSED"
            assert result.headers["cache-control"] == "no-store"
        assert client.get("/admin").status_code == 200
        assert app.state.db.one("SELECT COUNT(*) AS n FROM invocations")["n"] == 0
        assert app.state.db.one("SELECT COUNT(*) AS n FROM feed_views")["n"] == 0
        assert app.state.db.one("SELECT value FROM app_metadata WHERE key='provider_profile'") is None


def test_enabling_requires_real_credentials_and_binds_only_the_real_identity(tmp_path):
    with pytest.raises(ValueError, match="Runpod credentials"):
        config(tmp_path, inference_enabled=True).validate()
    real = config(tmp_path, inference_enabled=True, runpod_key="test-provider-key",
                  qwen_endpoint="qwen-endpoint", flux_endpoint="flux-endpoint",
                  start_coordinator=False)
    with TestClient(create_app(real)) as client:
        assert client.get("/health").json()["mode"] == "live"
    paused = create_app(config(tmp_path))
    assert paused.state.db.one("SELECT value FROM app_metadata WHERE key='provider_profile'")["value"] == real.provider_profile()
    # Pausing cannot make a previously bound database accept different endpoints.
    with pytest.raises(ValueError, match="endpoints differ"):
        create_app(config(tmp_path, inference_enabled=True, runpod_key="test-provider-key",
                          qwen_endpoint="different-endpoint", flux_endpoint="flux-endpoint"))


def test_pause_environment_is_explicit_and_strict(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("INFERENCE_ENABLED", "false")
    monkeypatch.setenv("PULSEFEED_FAKE_MODE", "false")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert Config.from_env().inference_enabled is False
    monkeypatch.setenv("INFERENCE_ENABLED", "perhaps")
    with pytest.raises(ValueError, match="INFERENCE_ENABLED"):
        Config.from_env()
