"""
Defensywny parser JSON odpowiedzi Gemini.

Problem: Gemini sporadycznie ignoruje `response_mime_type="application/json"`
i zwraca preambulę typu "Here is the JSON requested:\\n```json\\n{...}\\n```"
zamiast surowego JSON-a. Skutek: `model_validate_json()` crashuje.

Ten helper:
  1) zdejmuje markdown fences (```json ... ``` lub ``` ... ```)
  2) wycina zakres od pierwszego '{' (lub '[') do ostatniego '}' (lub ']')
  3) fallback: model_validate(json.loads(...))
  4) gdy raw jest pusty → rzuca ValueError (caller decyduje co zrobic)
"""
from __future__ import annotations

import json
import logging
from typing import Type, TypeVar

from pydantic import BaseModel

logger = logging.getLogger("json_parse")

T = TypeVar("T", bound=BaseModel)


def safe_model_validate_json(model_cls: Type[T], raw_text: str | None) -> T:
    """
    Parsuje `raw_text` jako JSON i waliduje do `model_cls`.
    Odporne na preambuly / markdown fences w odpowiedzi Gemini.

    Rzuca:
      - ValueError gdy raw jest pusty lub nie zawiera zadnego JSON-a
      - pydantic.ValidationError gdy JSON jest ok ale nie pasuje do schematu
    """
    raw = (raw_text or "").strip()
    if not raw:
        raise ValueError("Pusta odpowiedz od Gemini (prawdopodobnie token limit lub blad API).")

    # Zdejmij markdown fences
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    # Wytnij zakres od pierwszego nawiasu do ostatniego pasujacego
    if raw and not raw[0] in "{[":
        starts = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
        if not starts:
            raise ValueError(f"Brak poczatku JSON w odpowiedzi. Raw: {raw[:200]!r}")
        first = min(starts)
        # Dobierz pasujacy zamykajacy nawias
        ends = [raw.rfind("}"), raw.rfind("]")]
        last = max(ends)
        if last <= first:
            raise ValueError(f"Brak konca JSON w odpowiedzi. Raw: {raw[:200]!r}")
        raw = raw[first : last + 1]

    if not raw:
        raise ValueError("Po strippingu nie zostalo nic do parsowania.")

    # Primary path: bezposrednia walidacja
    try:
        return model_cls.model_validate_json(raw)
    except Exception as e_primary:
        logger.debug(f"[JSON_PARSE] Primary validate failed: {e_primary}. Trying json.loads fallback...")

    # Fallback: manual json.loads + model_validate (czasem obchodzi dziwne znaki escape)
    try:
        data = json.loads(raw)
        return model_cls.model_validate(data)
    except Exception as e_fallback:
        logger.warning(
            f"[JSON_PARSE] Both paths failed for {model_cls.__name__}. "
            f"Raw (first 300 chars): {raw[:300]!r}"
        )
        raise
