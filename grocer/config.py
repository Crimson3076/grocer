"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable {name}")
    return val


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Config:
    # Mealie
    mealie_url: str = field(default_factory=lambda: _env("MEALIE_URL", "http://192.168.0.83:3000").rstrip("/"))
    mealie_token: str = field(default_factory=lambda: _env("MEALIE_TOKEN", ""))
    # Address used for recipe links on the pages (what your phones open)
    mealie_public_url: str = field(default_factory=lambda: _env("MEALIE_PUBLIC_URL", "https://mealie.example.com").rstrip("/"))
    # Which meal plan entry types to shop for
    entry_types: list[str] = field(default_factory=lambda: _list("MEAL_ENTRY_TYPES", "dinner"))

    # Home Assistant
    ha_url: str = field(default_factory=lambda: _env("HA_URL", "http://192.168.0.61:8123").rstrip("/"))
    ha_token: str = field(default_factory=lambda: _env("HA_TOKEN", ""))
    notify_targets: list[str] = field(
        default_factory=lambda: _list("HA_NOTIFY_TARGETS", "")
    )

    # Ollama
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://192.168.0.83:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen3:8b"))

    # This service, as reachable from phones on the LAN (used in notification links)
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", "http://192.168.0.83:8790").rstrip("/"))
    vnc_url: str = field(default_factory=lambda: _env("VNC_URL", "http://192.168.0.83:6080/vnc.html"))

    # Behaviour
    default_servings: int = field(default_factory=lambda: int(_env("DEFAULT_SERVINGS", "2")))
    max_trusted_yield: int = field(default_factory=lambda: int(_env("MAX_TRUSTED_YIELD", "8")))
    order_minimum: float = field(default_factory=lambda: float(_env("ORDER_MINIMUM", "35")))
    auto_cart_if_unreviewed: bool = field(default_factory=lambda: _bool("AUTO_CART_IF_UNREVIEWED", False))
    tz: str = field(default_factory=lambda: _env("TZ", "America/New_York"))
    review_cron: str = field(default_factory=lambda: _env("REVIEW_CRON", "0 17 * * sat"))
    cart_cron: str = field(default_factory=lambda: _env("CART_CRON", "45 8 * * sun"))
    # plan empty dinner days (weighted toward recipes you haven't had in a while)
    plan_cron: str = field(default_factory=lambda: _env("PLAN_CRON", "5 15 * * sat"))
    dinner_tag: str = field(default_factory=lambda: _env("MEALIE_DINNER_TAG", "Dinner rotation"))
    # Pause between browser actions, seconds (keeps the session human paced)
    browser_delay: float = field(default_factory=lambda: float(_env("BROWSER_DELAY", "4")))
    browser_min_gap: float = field(default_factory=lambda: float(_env("BROWSER_MIN_GAP", "12")))
    cart_availability_minutes: float = field(default_factory=lambda: max(0.0, min(60.0, float(_env("CART_AVAILABILITY_MINUTES", "60")))))
    browser_channel: str = field(default_factory=lambda: _env("BROWSER_CHANNEL", "chrome"))
    walmart_store_id: str = field(default_factory=lambda: _env("WALMART_STORE_ID", ""))
    walmart_fulfillment: str = field(default_factory=lambda: _env("WALMART_FULFILLMENT", "PICKUP").upper())

    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "grocer.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"


CONFIG = Config()
