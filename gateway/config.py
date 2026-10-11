"""Load gateway settings from config.yaml + environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str | None = None
    timeout_s: float = 30.0
    stream_usage: bool = True  # ask for token usage in the final stream chunk
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.getenv(self.api_key_env) or None

    @property
    def configured(self) -> bool:
        """Local providers (no key needed) are always configured."""
        return self.api_key_env is None or self.api_key is not None


@dataclass(frozen=True)
class Deployment:
    """One concrete place a request can go: a model on a provider."""

    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class Price:
    input_per_m: float
    output_per_m: float


@dataclass
class RoutingConfig:
    long_prompt_chars: int = 4000
    long_conversation_turns: int = 6
    smart_keywords: list[str] = field(default_factory=list)


@dataclass
class Settings:
    providers: dict[str, ProviderConfig]
    aliases: dict[str, list[Deployment]]
    prices: dict[str, Price] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    max_retries: int = 2
    backoff_base_s: float = 0.5
    cooldown_s: float = 30.0
    cache_ttl_s: float = 3600.0
    cache_max_entries: int = 1000
    db_path: str = "gateway.db"
    gateway_api_key: str | None = None


def settings_from_dict(raw: dict[str, Any]) -> Settings:
    providers = {
        name: ProviderConfig(name=name, **(cfg or {}))
        for name, cfg in (raw.get("providers") or {}).items()
    }
    aliases = {
        alias: [Deployment(provider=d["provider"], model=str(d["model"])) for d in chain]
        for alias, chain in (raw.get("aliases") or {}).items()
    }
    for alias, chain in aliases.items():
        for dep in chain:
            if dep.provider not in providers:
                raise ValueError(f"alias '{alias}' uses unknown provider '{dep.provider}'")

    prices = {
        key: Price(input_per_m=float(p["input"]), output_per_m=float(p["output"]))
        for key, p in (raw.get("prices") or {}).items()
    }
    reliability = raw.get("reliability") or {}
    cache = raw.get("cache") or {}

    return Settings(
        providers=providers,
        aliases=aliases,
        prices=prices,
        routing=RoutingConfig(**(raw.get("routing") or {})),
        max_retries=int(reliability.get("max_retries", 2)),
        backoff_base_s=float(reliability.get("backoff_base_s", 0.5)),
        cooldown_s=float(reliability.get("cooldown_s", 30)),
        cache_ttl_s=float(cache.get("ttl_s", 3600)),
        cache_max_entries=int(cache.get("max_entries", 1000)),
        db_path=str(raw.get("db_path", "gateway.db")),
        gateway_api_key=os.getenv("GATEWAY_API_KEY") or None,
    )


def load_settings(path: str | Path | None = None) -> Settings:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    path = Path(path or os.getenv("GATEWAY_CONFIG") or PROJECT_ROOT / "config.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = settings_from_dict(raw)
    if not Path(settings.db_path).is_absolute():
        settings.db_path = str(PROJECT_ROOT / settings.db_path)
    return settings
