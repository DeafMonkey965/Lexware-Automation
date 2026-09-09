from __future__ import annotations

import os
from dataclasses import dataclass


def enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


@dataclass(frozen=True)
class LocalConfig:
    use_openai_fallback: bool = False
    use_ollama: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    save_ocr_text: bool = False

    @classmethod
    def from_env(cls) -> LocalConfig:
        return cls(enabled("USE_OPENAI_FALLBACK"), enabled("USE_OLLAMA"),
                   os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                   os.getenv("OLLAMA_MODEL", ""), enabled("SAVE_OCR_TEXT"))
