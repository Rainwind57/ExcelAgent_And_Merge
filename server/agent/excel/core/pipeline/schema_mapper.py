from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


def normalize_field_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[:：].*$", "", text)
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\([^)]*\)", "", text)
    return re.sub(r"[\s_\-./\[\]()（）:：]+", "", text)


def _type_base(value: Any) -> str:
    text = str(value or "").strip()
    if ":" in text:
        text = text.split(":", 1)[0]
    if "：" in text:
        text = text.split("：", 1)[0]
    return text.strip()


def build_schema_field_map(headers: list | None,
                           type_row: list | None = None) -> dict[str, str]:
    headers = headers or []
    type_row = type_row or []
    field_map: dict[str, str] = {}
    for idx in range(max(len(headers), len(type_row))):
        header = str(headers[idx] if idx < len(headers) else "" or "").strip()
        typ = str(type_row[idx] if idx < len(type_row) else "" or "").strip()
        canonical = _type_base(typ) or header
        if not canonical:
            continue
        variants = {
            header,
            canonical,
            _type_base(header),
            typ,
        }
        for value in variants:
            norm = normalize_field_name(value)
            if norm:
                field_map.setdefault(norm, canonical)
        last = canonical.split(".")[-1]
        if last and last != canonical:
            field_map.setdefault(normalize_field_name(last), canonical)
    return field_map


def remap_fields_to_schema(fields: dict,
                           headers: list | None,
                           type_row: list | None = None,
                           *,
                           fuzzy_threshold: float = 0.92) -> tuple[dict, dict]:
    if not isinstance(fields, dict):
        return {}, {"renames": {}, "unmapped": [], "collisions": []}
    field_map = build_schema_field_map(headers, type_row)
    remapped: dict[str, Any] = {}
    renames: dict[str, str] = {}
    unmapped: list[str] = []
    collisions: list[dict] = []
    norms = list(field_map)

    def resolve(col: str) -> str:
        norm = normalize_field_name(col)
        if norm in field_map:
            return field_map[norm]
        if norm and norms:
            best = max(norms, key=lambda x: SequenceMatcher(None, norm, x).ratio())
            score = SequenceMatcher(None, norm, best).ratio()
            if score >= fuzzy_threshold:
                return field_map[best]
        return ""

    for col, value in fields.items():
        col_s = str(col)
        mapped = resolve(col_s)
        out_col = mapped or col_s
        if mapped and mapped != col_s:
            renames[col_s] = mapped
        if not mapped:
            unmapped.append(col_s)
        if out_col in remapped:
            collisions.append({
                "column": out_col,
                "old_value": remapped[out_col],
                "new_value": value,
                "source": col_s,
            })
        remapped[out_col] = value
    return remapped, {
        "renames": renames,
        "unmapped": unmapped,
        "collisions": collisions,
    }
