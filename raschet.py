from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "dannye"
API = "https://fgis.gost.ru/fundmetrology/eapi/vri"
YEARS = (2023, 2024, 2025)
MODELS = {
    "altair": {"search": "71431-18"},
    "eris414": {"mit_number": "65108-16", "mi_modification": "*414*"},
}
CYRILLIC = str.maketrans({
    "А": "A", "Б": "B", "В": "B", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "ZH", "З": "Z", "И": "I", "Й": "I", "К": "K", "Л": "L", "М": "M",
    "Н": "H", "О": "O", "П": "P", "Р": "R", "С": "C", "Т": "T", "У": "Y",
    "Ф": "F", "Х": "H", "Ц": "C", "Ч": "CH", "Ш": "SH", "Щ": "SCH",
    "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "U", "Я": "YA",
})


def get_json(params: dict[str, object]) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def get_page(params: dict[str, object], attempts: int = 7) -> dict:
    last_error = None
    for attempt in range(attempts):
        size = 100 - attempt
        start = int(params["start"])
        try:
            result = get_json({**params, "rows": size})["result"]
            if attempt:
                tail = get_json({**params, "rows": attempt, "start": start + size})["result"]
                result["items"].extend(tail["items"])
            return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(min(2 ** attempt, 20) + random.random())
    raise RuntimeError(f"Не удалось получить страницу start={params['start']}: {last_error}")


def download_model(model: str, year: int) -> dict:
    params = {"year": year, "rows": 100, "start": 0, **MODELS[model]}
    first = get_page(params)
    count = int(first["count"])
    items = list(first["items"])
    starts = list(range(100, count, 100))
    if starts:
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = list(pool.map(
                lambda start: get_page({**params, "start": start})["items"],
                starts,
            ))
        for page in pages:
            items.extend(page)
    if len(items) < count:
        raise RuntimeError(f"{model} {year}: API count={count}, получено позиций={len(items)}")
    unique = {item["vri_id"]: item for item in items}
    result = {
        "source": API + "?" + urllib.parse.urlencode(params),
        "retrieved": date.today().isoformat(),
        "api_count": count,
        "received_count": len(items),
        "unique_count": len(unique),
        "duplicate_count": len(items) - len(unique),
        "items": list(unique.values()),
    }
    path = DATA / f"{model}-{year}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{model} {year}: {len(unique)} уникальных из {count} позиций API", flush=True)
    return result


def load_model(model: str, year: int, refresh: bool) -> dict:
    path = DATA / f"{model}-{year}.json"
    if path.exists() and not refresh:
        result = json.loads(path.read_text(encoding="utf-8"))
        if "received_count" not in result:
            result["received_count"] = result["api_count"]
            result["duplicate_count"] = result["received_count"] - result["unique_count"]
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    return download_model(model, year)


def excluded(org: str) -> bool:
    value = org.upper()
    return "ОЛДИС" in value or "ЭРИС" in value


def counts(items: list[dict]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for item in items:
        org = item["org_title"]
        result[org][1] += 1
        if not item["applicability"]:
            result[org][0] += 1
    return result


def cmh(rows: list[dict]) -> dict:
    expected = 0.0
    observed = 0.0
    variance = 0.0
    or_num = 0.0
    or_den = 0.0
    for row in rows:
        a = row["altair_bad"]
        b = row["altair_total"] - a
        c = row["eris414_bad"]
        d = row["eris414_total"] - c
        n = a + b + c + d
        observed += a
        expected += (a + b) * (a + c) / n
        if n > 1:
            variance += (a + b) * (c + d) * (a + c) * (b + d) / (n * n * (n - 1))
        or_num += a * d / n
        or_den += b * c / n
    statistic = (observed - expected) ** 2 / variance if variance else None
    p_value = math.erfc(math.sqrt(statistic / 2)) if statistic is not None else None
    return {
        "strata": len(rows),
        "altair_bad": sum(row["altair_bad"] for row in rows),
        "altair_total": sum(row["altair_total"] for row in rows),
        "eris414_bad": sum(row["eris414_bad"] for row in rows),
        "eris414_total": sum(row["eris414_total"] for row in rows),
        "or_mh": or_num / or_den if or_den else None,
        "cmh": statistic,
        "p_cmh": p_value,
    }


def normalize_modification(value: str | None) -> str:
    text = (value or "").upper()
    has_411 = "411" in text
    has_414 = "414" in text
    if has_411 and has_414:
        return "ambiguous"
    if has_414:
        return "414"
    if has_411:
        return "411"
    return "other"


def normalize_serial(value: str | None) -> str:
    text = (value or "").upper().translate(CYRILLIC)
    return "".join(re.findall(r"[A-Z0-9]+", text))


def normalization_report(eris_items: list[dict]) -> dict:
    modifications = Counter((item.get("mi_modification") or "").strip() for item in eris_items)
    valid = [
        item for item in eris_items
        if normalize_modification(item.get("mi_modification")) == "414"
        and len(normalize_serial(item.get("mi_number"))) >= 4
        and not any(word in (item.get("mi_number") or "").upper() for word in ("НЕВОЗМОЖНО", "ОТСУТСТВУЕТ", "НЕТ"))
    ]
    raw_groups: dict[str, list[dict]] = defaultdict(list)
    normalized_groups: dict[str, list[dict]] = defaultdict(list)
    for item in valid:
        raw = (item.get("mi_number") or "").strip()
        raw_groups[raw].append(item)
        normalized_groups[normalize_serial(raw)].append(item)
    merged = []
    for serial, group in normalized_groups.items():
        raw_values = sorted({(item.get("mi_number") or "").strip() for item in group})
        if len(raw_values) > 1:
            merged.append({
                "normalized_serial": serial,
                "raw_values": raw_values,
                "vri_id": [item["vri_id"] for item in group[:10]],
            })
    cyrillic = [item for item in valid if (item.get("mi_number") or "").upper() != (item.get("mi_number") or "").upper().translate(CYRILLIC)]
    prefix_examples = []
    for item in valid:
        raw = (item.get("mi_number") or "").strip()
        upper = raw.upper()
        if ("ЕР414" in upper or "ER414" in upper) and len(prefix_examples) < 20:
            prefix_examples.append({
                "raw": raw,
                "normalized": normalize_serial(raw),
                "vri_id": item["vri_id"],
            })
    return {
        "method": "Модификация определяется по 411/414; номер приводится к верхнему регистру, кириллические двойники заменяются латиницей, разделители удаляются.",
        "records_considered": len(eris_items),
        "valid_serial_records": len(valid),
        "modification_top": modifications.most_common(30),
        "modification_normalized": dict(Counter(normalize_modification(item.get("mi_modification")) for item in eris_items)),
        "raw_unique_serials": len(raw_groups),
        "normalized_unique_serials": len(normalized_groups),
        "raw_multi_record_serials": sum(len(group) > 1 for group in raw_groups.values()),
        "normalized_multi_record_serials": sum(len(group) > 1 for group in normalized_groups.values()),
        "merged_groups": len(merged),
        "merged_examples": merged[:20],
        "cyrillic_changed_records": len(cyrillic),
        "prefix_examples": prefix_examples,
        "synthetic_pair": {
            "cyrillic": "ЕР414-00123",
            "latin": "ER414 00123",
            "normalized_cyrillic": normalize_serial("ЕР414-00123"),
            "normalized_latin": normalize_serial("ER414 00123"),
            "equal": normalize_serial("ЕР414-00123") == normalize_serial("ER414 00123"),
        },
        "limitation": "Совпадение нормализованного номера — кандидат на связь, а не доказанная идентичность экземпляра. Точность связей здесь не оценивается.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    loaded = {
        (model, year): load_model(model, year, args.refresh)
        for year in YEARS for model in MODELS
    }
    rows = []
    for year in YEARS:
        altair = counts(loaded[("altair", year)]["items"])
        eris_items = [item for item in loaded[("eris414", year)]["items"] if normalize_modification(item.get("mi_modification")) == "414"]
        eris = counts(eris_items)
        for org in sorted(set(altair) & set(eris)):
            if excluded(org):
                continue
            a_bad, a_total = altair[org]
            e_bad, e_total = eris[org]
            if a_total >= 15 and e_total >= 15:
                rows.append({
                    "year": year,
                    "org_title": org,
                    "altair_bad": a_bad,
                    "altair_total": a_total,
                    "eris414_bad": e_bad,
                    "eris414_total": e_total,
                    "source_altair": loaded[("altair", year)]["source"],
                    "source_eris414": loaded[("eris414", year)]["source"],
                })
    with (DATA / "straty-eris-414.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "retrieved": date.today().isoformat(),
        "rule": "Страта — поверитель и год; у каждой модели не менее 15 записей; исключены организации с «Олдис» или «ЭРИС» в названии и записи, где одновременно указаны 411 и 414. CMH без поправки на непрерывность, OR Мантеля — Хензеля.",
        "eris414_ambiguous_records_excluded": sum(normalize_modification(item.get("mi_modification")) == "ambiguous" for year in YEARS for item in loaded[("eris414", year)]["items"]),
        "years": {str(year): cmh([row for row in rows if row["year"] == year]) for year in YEARS},
        "combined": cmh(rows),
        "downloads": {
            f"{model}-{year}": {
                "source": loaded[(model, year)]["source"],
                "retrieved": loaded[(model, year)]["retrieved"],
                "api_count": loaded[(model, year)]["api_count"],
                "received_count": loaded[(model, year)]["received_count"],
                "unique_count": loaded[(model, year)]["unique_count"],
                "duplicate_count": loaded[(model, year)]["duplicate_count"],
            }
            for model, year in loaded
        },
        "limitation": "Наблюдаемые исходы поверок, не причинная надёжность. Владелец, возраст и замена сенсоров не учтены.",
    }
    (DATA / "svodka-eris-414.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    all_eris = [item for year in YEARS for item in loaded[("eris414", year)]["items"]]
    normalization = normalization_report(all_eris)
    (DATA / "normalizatsiya.json").write_text(json.dumps(normalization, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary["years"], ensure_ascii=False, indent=2), flush=True)
    print(json.dumps(summary["combined"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
