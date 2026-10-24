"""Scientific image generation for the SmartLab Visualizer.

Two providers are supported, chosen by ``SMARTLAB_IMAGE_PROVIDER`` (``auto`` picks the first one with a key):

* OpenAI Images API (``OPENAI_API_KEY``), model ``SMARTLAB_OPENAI_IMAGE_MODEL`` (default ``gpt-image-1``).
* Google Imagen via the Gemini API (``GOOGLE_API_KEY`` or ``GEMINI_API_KEY``), model ``SMARTLAB_IMAGEN_MODEL``.

API keys are read from the environment only and never logged or returned. Provider output is re-encoded
as PNG here, so whatever the provider sent (metadata, a polyglot file) never reaches the client verbatim.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import unicodedata
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

STYLES = {
    "diagram": "Clean scientific schematic diagram, flat vector style, white background, clear arrows and "
               "distinct colour-coded components, textbook figure quality.",
    "illustration": "Detailed scientific illustration, journal cover quality, accurate proportions, soft "
                    "studio lighting, neutral background.",
    "photorealistic": "Photorealistic laboratory photograph, accurate equipment and materials, natural "
                      "lighting, shallow depth of field.",
}
ASPECT_RATIOS = ("1:1", "16:9", "9:16")
MIN_PROMPT_CHARS = 8
MAX_PROMPT_CHARS = 1000

# Image models render text poorly; asking for no labels gives cleaner figures that users annotate themselves.
_PREAMBLE = ("Create an accurate, professional scientific visual for researchers. Do not render text, labels, "
             "watermarks or logos. Subject: ")

# Requests for imagery that would work as weapons or attack guidance. Image providers moderate too; this
# refuses before a key-bearing request is made and gives the user a reason they can act on.
_PROHIBITED = re.compile(
    r"\b(sarin|soman|tabun|vx\s*(nerve|agent|gas)|novichok|nerve\s*agent|mustard\s*gas|ricin|abrin|anthrax\s*(spore|weapon)"
    r"|weaponi[sz]\w*|bioweapon\w*|chemical\s*weapon\w*|pipe\s*bomb|ied\b|detonator|tatp|hmtd|nitroglycerin\s*synthesis"
    r"|gain[\s-]*of[\s-]*function|enhance\w*\s+(transmissibility|virulence|lethality))\b",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ImageGenUnavailable(RuntimeError):
    """No provider is configured, or the provider could not be reached / failed."""


class ImageGenRejected(ValueError):
    """The prompt was refused, by our screen or by the provider's content policy."""


@dataclass(frozen=True)
class GeneratedImage:
    png: bytes
    width: int
    height: int
    final_prompt: str
    provider: str
    model: str


def sanitize_prompt(raw: object) -> str:
    """Normalise a user prompt: NFC, no control characters, collapsed whitespace, length-checked."""
    if not isinstance(raw, str):
        raise ImageGenRejected("prompt must be text.")
    text = _CONTROL.sub(" ", unicodedata.normalize("NFC", raw))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < MIN_PROMPT_CHARS:
        raise ImageGenRejected(f"Describe the visual in at least {MIN_PROMPT_CHARS} characters.")
    if len(text) > MAX_PROMPT_CHARS:
        raise ImageGenRejected(f"Prompts are limited to {MAX_PROMPT_CHARS} characters.")
    if _PROHIBITED.search(text):
        raise ImageGenRejected("This prompt describes weapons or harmful agents, which the Visualizer cannot depict.")
    return text


def build_prompt(prompt: str, style: str) -> str:
    return f"{_PREAMBLE}{prompt}\nStyle: {STYLES[style]}"


def _reencode(data: bytes, max_pixels: int) -> tuple[bytes, int, int]:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > max_pixels:
                raise ImageGenUnavailable("The provider returned an oversized image.")
            img.load()
            out = io.BytesIO()
            img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(out, format="PNG", optimize=True)
            return out.getvalue(), img.width, img.height
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageGenUnavailable("The provider returned an unreadable image.") from exc


class ImageGenerator:
    def __init__(self, provider: str = "auto", timeout_s: float = 120.0, max_pixels: int = 40_000_000):
        self.timeout_s = timeout_s
        self.max_pixels = max_pixels
        self._openai_key = os.getenv("OPENAI_API_KEY") or None
        self._google_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or None
        self.openai_model = os.getenv("SMARTLAB_OPENAI_IMAGE_MODEL", "gpt-image-1")
        self.imagen_model = os.getenv("SMARTLAB_IMAGEN_MODEL", "imagen-4.0-generate-001")
        provider = (provider or "auto").strip().lower()
        if provider == "auto":
            provider = "openai" if self._openai_key else "google" if self._google_key else "off"
        elif (provider == "openai" and not self._openai_key) or (provider == "google" and not self._google_key):
            logger.warning("Image provider %r selected but its API key is not set; the Visualizer is disabled.", provider)
            provider = "off"
        elif provider not in ("openai", "google"):
            provider = "off"
        self.provider = provider

    @property
    def available(self) -> bool:
        return self.provider != "off"

    @property
    def model(self) -> str | None:
        return {"openai": self.openai_model, "google": self.imagen_model}.get(self.provider)

    def generate(self, prompt: str, style: str = "diagram", aspect_ratio: str = "1:1") -> GeneratedImage:
        if not self.available:
            raise ImageGenUnavailable("Image generation is not configured (set OPENAI_API_KEY or GOOGLE_API_KEY).")
        final_prompt = build_prompt(prompt, style)
        try:
            raw = self._openai(final_prompt, aspect_ratio) if self.provider == "openai" else self._imagen(final_prompt, aspect_ratio)
        except requests.Timeout as exc:
            raise ImageGenUnavailable("The image provider timed out. Try again or simplify the prompt.") from exc
        except requests.RequestException as exc:
            raise ImageGenUnavailable("Could not reach the image provider.") from exc
        png, width, height = _reencode(raw, self.max_pixels)
        return GeneratedImage(png, width, height, final_prompt, self.provider, self.model)

    # ------------------------------------------------------------------ providers
    def _openai(self, prompt: str, aspect_ratio: str) -> bytes:
        dalle = self.openai_model.startswith("dall-e")
        size = {"1:1": "1024x1024", "16:9": "1792x1024" if dalle else "1536x1024",
                "9:16": "1024x1792" if dalle else "1024x1536"}[aspect_ratio]
        body: dict = {"model": self.openai_model, "prompt": prompt, "size": size, "n": 1}
        if dalle:  # gpt-image models always return base64 and reject response_format
            body["response_format"] = "b64_json"
        resp = requests.post("https://api.openai.com/v1/images/generations", json=body, timeout=self.timeout_s,
                             headers={"Authorization": f"Bearer {self._openai_key}"})
        if resp.status_code != 200:
            error = _json(resp).get("error") or {}
            if error.get("code") in ("moderation_blocked", "content_policy_violation"):
                raise ImageGenRejected("The image provider declined this prompt under its content policy.")
            self._raise_status("OpenAI", resp.status_code, error.get("message"))
        try:
            return base64.b64decode(_json(resp)["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ImageGenUnavailable("The image provider returned an unexpected response.") from exc

    def _imagen(self, prompt: str, aspect_ratio: str) -> bytes:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.imagen_model}:predict"
        body = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio}}
        resp = requests.post(url, json=body, timeout=self.timeout_s, headers={"x-goog-api-key": self._google_key})
        if resp.status_code != 200:
            self._raise_status("Imagen", resp.status_code, (_json(resp).get("error") or {}).get("message"))
        predictions = _json(resp).get("predictions") or []
        encoded = next((p.get("bytesBase64Encoded") for p in predictions if p.get("bytesBase64Encoded")), None)
        if not encoded:  # Imagen returns no image (or only an raiFilteredReason) when its safety filter triggers
            raise ImageGenRejected("The image provider declined this prompt under its content policy.")
        try:
            return base64.b64decode(encoded)
        except ValueError as exc:
            raise ImageGenUnavailable("The image provider returned an unexpected response.") from exc

    @staticmethod
    def _raise_status(name: str, status: int, message: str | None) -> None:
        logger.warning("%s image API error %s: %s", name, status, (message or "")[:300])
        if status in (401, 403):
            raise ImageGenUnavailable("The image provider rejected the configured API key.")
        if status == 429:
            raise ImageGenUnavailable("The image provider's rate limit was reached. Try again in a minute.")
        if status == 400:
            raise ImageGenRejected("The image provider could not process this prompt. Try rephrasing it.")
        raise ImageGenUnavailable(f"The image provider failed ({status}).")


def _json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
