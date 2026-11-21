"""Loads the simulated knowledge base once."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"

COUNTRY_ALIASES = {
    "us": "US", "usa": "US", "united states": "US", "america": "US", "the states": "US",
    "ca": "CA", "canada": "CA",
    "gb": "GB", "uk": "GB", "united kingdom": "GB", "england": "GB", "britain": "GB", "london": "GB", "scotland": "GB",
    "de": "DE", "germany": "DE", "deutschland": "DE", "berlin": "DE", "munich": "DE",
    "fr": "FR", "france": "FR", "paris": "FR",
    "au": "AU", "australia": "AU", "sydney": "AU", "melbourne": "AU",
    "jp": "JP", "japan": "JP", "tokyo": "JP", "osaka": "JP",
    "br": "BR", "brazil": "BR", "brasil": "BR", "sao paulo": "BR",
    "mx": "MX", "mexico": "MX",
    "ru": "RU", "russia": "RU", "moscow": "RU",
    "kp": "KP", "north korea": "KP", "ir": "IR", "iran": "IR",
}


@lru_cache(maxsize=1)
def catalog() -> list[dict]:
    return json.loads((DATA / "catalog.json").read_text())


@lru_cache(maxsize=1)
def catalog_by_sku() -> dict[str, dict]:
    return {p["sku"]: p for p in catalog()}


@lru_cache(maxsize=1)
def merchants() -> dict[str, dict]:
    return json.loads((DATA / "merchants.json").read_text())


@lru_cache(maxsize=1)
def shipping_rules() -> dict:
    return json.loads((DATA / "shipping_rules.json").read_text())


def normalize_country(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[v]
    if len(v) == 2 and v.upper() in shipping_rules()["countries"]:
        return v.upper()
    return None


def country_name(code: str | None) -> str:
    if not code:
        return "your country"
    c = shipping_rules()["countries"].get(code)
    if c:
        return c["name"]
    return {"RU": "Russia", "KP": "North Korea", "IR": "Iran"}.get(code, code)
