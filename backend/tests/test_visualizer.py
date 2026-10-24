"""SmartLab Visualizer: prompt screening, provider adapters, the generation log and workspace actions."""
import base64
import io

import pytest
from PIL import Image

from app.services import imagegen
from app.services.imagegen import (GeneratedImage, ImageGenerator, ImageGenRejected, ImageGenUnavailable,
                                   sanitize_prompt)


def _png(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


class FakeGenerator:
    available = True
    provider = "openai"
    model = "fake-image-model"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def generate(self, prompt, style="diagram", aspect_ratio="1:1"):
        self.calls.append((prompt, style, aspect_ratio))
        if self.error:
            raise self.error
        return GeneratedImage(_png(), 64, 48, imagegen.build_prompt(prompt, style), self.provider, self.model)


@pytest.fixture()
def fake_gen(app):
    def _install(gen=None):
        gen = gen or FakeGenerator()
        app.extensions["smartlab"]["imagegen"] = gen
        return gen
    return _install


def _generate(client, prompt="CRISPR-Cas9 gene editing mechanism", **extra):
    return client.post("/api/visualizer/generate", json={"prompt": prompt, **extra})


# ------------------------------------------------------------------ prompt screening
def test_sanitize_prompt_normalises_and_bounds():
    assert sanitize_prompt("  Fume\x00 hood\n\n vapour   flow  ") == "Fume hood vapour flow"
    with pytest.raises(ImageGenRejected):
        sanitize_prompt("cell")
    with pytest.raises(ImageGenRejected):
        sanitize_prompt("x" * 1001)
    with pytest.raises(ImageGenRejected):
        sanitize_prompt(42)


@pytest.mark.parametrize("prompt", ["Step-by-step sarin synthesis apparatus", "How to weaponize anthrax spores",
                                    "Pipe bomb assembly diagram"])
def test_sanitize_prompt_refuses_weapons(prompt):
    with pytest.raises(ImageGenRejected):
        sanitize_prompt(prompt)


# ------------------------------------------------------------------ provider adapters
class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def test_openai_adapter_decodes_and_reencodes(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = {}

    def post(url, json, timeout, headers):
        sent.update(url=url, body=json, headers=headers)
        return _Resp(200, {"data": [{"b64_json": base64.b64encode(_png((32, 32))).decode()}]})

    monkeypatch.setattr(imagegen.requests, "post", post)
    gen = ImageGenerator("auto")
    result = gen.generate("Phospholipid bilayer cross-section", "diagram", "16:9")
    assert gen.provider == "openai" and sent["body"]["size"] == "1536x1024" and "response_format" not in sent["body"]
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert result.png.startswith(b"\x89PNG") and (result.width, result.height) == (32, 32)


def test_openai_moderation_is_a_rejection(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(imagegen.requests, "post", lambda *a, **k: _Resp(400, {"error": {"code": "moderation_blocked"}}))
    with pytest.raises(ImageGenRejected):
        ImageGenerator("openai").generate("Mitochondrial electron transport chain")


def test_imagen_adapter_and_safety_filter(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    png = base64.b64encode(_png()).decode()
    monkeypatch.setattr(imagegen.requests, "post", lambda *a, **k: _Resp(200, {"predictions": [{"bytesBase64Encoded": png}]}))
    gen = ImageGenerator("auto")
    assert gen.provider == "google" and gen.generate("Rotary evaporator setup").width == 64
    monkeypatch.setattr(imagegen.requests, "post", lambda *a, **k: _Resp(200, {"predictions": [{"raiFilteredReason": "x"}]}))
    with pytest.raises(ImageGenRejected):
        gen.generate("Rotary evaporator setup")


def test_provider_errors_and_missing_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert not ImageGenerator("auto").available and not ImageGenerator("openai").available
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(imagegen.requests, "post", lambda *a, **k: _Resp(401, {"error": {"message": "bad key"}}))
    with pytest.raises(ImageGenUnavailable):
        ImageGenerator("openai").generate("Western blot transfer stack")


# ------------------------------------------------------------------ API
def test_info_and_disabled_generation(client):
    info = client.get("/api/visualizer/info").get_json()
    assert info["enabled"] is False and "diagram" in info["styles"]
    assert _generate(client).status_code == 503


def test_generate_stores_image_logs_and_audits(client, fake_gen, conn):
    gen = fake_gen()
    resp = _generate(client, style="illustration", aspect_ratio="16:9")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["status"] == "succeeded" and body["image_url"] == f"/api/visualizer/images/{body['id']}/file"
    assert gen.calls == [("CRISPR-Cas9 gene editing mechanism", "illustration", "16:9")]
    assert "Do not render text" in body["final_prompt"] and len(body["sha256"]) == 64

    img = client.get(body["image_url"])
    assert img.status_code == 200 and img.mimetype == "image/png" and img.data.startswith(b"\x89PNG")
    assert img.headers["X-Content-Type-Options"] == "nosniff" and "sandbox" in img.headers["Content-Security-Policy"]
    assert "attachment" in client.get(body["image_url"] + "?download=1").headers["Content-Disposition"]

    audit = conn.execute("SELECT action, entity_id FROM audit_log WHERE entity_type = 'generated_image'").fetchall()
    assert [tuple(a) for a in audit] == [("image.succeeded", body["id"])]
    assert [i["id"] for i in client.get("/api/visualizer/images").get_json()] == [body["id"]]


def test_refused_and_failed_attempts_are_logged(client, fake_gen):
    fake_gen()
    blocked = _generate(client, prompt="Nerve agent dispersal device")
    assert blocked.status_code == 422 and blocked.get_json()["image"]["status"] == "blocked"

    fake_gen(FakeGenerator(ImageGenRejected("The image provider declined this prompt under its content policy.")))
    assert _generate(client).get_json()["image"]["status"] == "blocked"
    fake_gen(FakeGenerator(ImageGenUnavailable("The image provider timed out.")))
    failed = _generate(client)
    assert failed.status_code == 502 and failed.get_json()["image"]["image_url"] is None

    assert client.get("/api/visualizer/images").get_json() == []
    attempts = client.get("/api/visualizer/images?all=true").get_json()
    assert [a["status"] for a in attempts] == ["failed", "blocked", "blocked"]
    assert client.get(f"/api/visualizer/images/{attempts[0]['id']}/file").status_code == 409


def test_generate_validation(client, fake_gen):
    fake_gen()
    assert client.post("/api/visualizer/generate", json={}).status_code == 400
    assert _generate(client, style="cubist").status_code == 422
    assert _generate(client, aspect_ratio="4:3").status_code == 422
    assert _generate(client, project_id=99999).status_code == 404


def test_generation_log_is_append_only(client, fake_gen, conn):
    fake_gen()
    image_id = _generate(client).get_json()["id"]
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE generated_images SET prompt = 'edited' WHERE id = ?", (image_id,))
    conn.rollback()
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM generated_images WHERE id = ?", (image_id,))


def test_save_to_project_and_attach_to_experiment(client, fake_gen):
    fake_gen()
    image_id = _generate(client).get_json()["id"]
    project_id = client.get("/api/projects").get_json()[0]["id"]

    saved = client.post(f"/api/visualizer/images/{image_id}/save-to-project", json={"project_id": project_id})
    assert saved.status_code == 201 and saved.get_json()["original_name"].startswith(f"visualizer-{image_id}-crispr")
    files = client.get(f"/api/projects/{project_id}/files").get_json()
    assert client.get(f"/api/files/{files[0]['id']}").data.startswith(b"\x89PNG")
    assert client.post(f"/api/visualizer/images/{image_id}/save-to-project", json={"project_id": 99999}).status_code == 404

    exp_id = client.get("/api/experiments").get_json()[0]["id"]
    before = client.get(f"/api/experiments/{exp_id}").get_json()["protocol_markdown"]
    attached = client.post(f"/api/visualizer/images/{image_id}/attach-to-experiment", json={"experiment_id": exp_id})
    assert attached.status_code == 200
    after = client.get(f"/api/experiments/{exp_id}").get_json()["protocol_markdown"]
    assert after.startswith(before.rstrip()) and f"](/api/visualizer/images/{image_id}/file)" in after
    assert "not experimental data" in after
