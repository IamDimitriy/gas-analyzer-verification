from __future__ import annotations

import csv
import hashlib
import json
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


def get_card(vri_id: str, attempts: int = 7) -> dict:
    url = f"{API}/{urllib_request_quote(vri_id)}"
    last_error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(min(2 ** attempt, 20) + random.random())
    raise RuntimeError(f"Не удалось получить {url}: {last_error}")


def urllib_request_quote(value: str) -> str:
    return urllib.parse.quote(value, safe="-.")


def normalize_owner(value: str | None) -> str:
    text = (value or "").upper().replace("«", '"').replace("»", '"')
    return re.sub(r"\s+", " ", text).strip()


def owner_group(value: str | None) -> str | None:
    text = normalize_owner(value)
    if "ИНК-СЕРВИС" in text:
        return "ink-service"
    if "ИРКУТСКАЯ НЕФТЯНАЯ КОМПАНИЯ" in text or '"ИНК"' in text:
        return "ink"
    if "ОЭМК" in text or "ОСКОЛЬСКИЙ ЭЛЕКТРОМЕТАЛЛУРГИЧЕСКИЙ КОМБИНАТ" in text:
        return "oemk"
    if "АПАТИТ" in text and "КИРОВСК" in text:
        return "apatit"
    return None


def select_sample(items: list[dict], org: str, year: int, model: str, size: int = 15) -> list[dict]:
    selected = sorted((item for item in items if item["org_title"] == org), key=lambda item: item["vri_id"])
    seed = int(hashlib.sha256(f"kirill-zadel|{year}|{org}|{model}".encode()).hexdigest()[:16], 16)
    return random.Random(seed).sample(selected, min(size, len(selected)))


def main() -> None:
    rows = list(csv.DictReader((DATA / "straty-eris-414.csv").open(encoding="utf-8-sig")))
    combined = Counter()
    for row in rows:
        combined[row["org_title"]] += int(row["altair_total"]) + int(row["eris414_total"])
    top_orgs = [org for org, _ in combined.most_common(5)]
    loaded = {
        (model, year): json.loads((DATA / f"{model}-{year}.json").read_text(encoding="utf-8"))["items"]
        for model in ("altair", "eris414") for year in YEARS
    }
    for year in YEARS:
        loaded[("eris414", year)] = [
            item for item in loaded[("eris414", year)]
            if "414" in (item.get("mi_modification") or "") and "411" not in (item.get("mi_modification") or "")
        ]
    sample = []
    for year in YEARS:
        for org in top_orgs:
            for model in ("altair", "eris414"):
                for item in select_sample(loaded[(model, year)], org, year, model):
                    sample.append({"model": model, "year": year, "org_title": org, **item})
    with ThreadPoolExecutor(max_workers=6) as pool:
        cards = list(pool.map(lambda item: get_card(item["vri_id"]), sample))
    records = []
    for item, card in zip(sample, cards):
        result = card["result"]
        vri = result["vriInfo"]
        single = result["miInfo"]["singleMI"]
        records.append({
            **item,
            "miOwner": vri.get("miOwner"),
            "owner_normalized": normalize_owner(vri.get("miOwner")),
            "owner_group": owner_group(vri.get("miOwner")),
            "vriType": vri.get("vriType"),
            "card_modification": single.get("modification"),
            "card_serial": single.get("manufactureNum"),
            "card_url": f"{API}/{item['vri_id']}",
        })
    source = {
        "retrieved": date.today().isoformat(),
        "method": "Пять крупнейших страт по объёму пары за 2023–2025 годы; в каждой страте и году до 15 записей каждой модели, фиксированное зерно выбора. Это стратифицированная случайная выборка карточек, не полный массив владельцев.",
        "top_orgs": top_orgs,
        "sample_size": len(records),
        "records": records,
    }
    (DATA / "vladeltsy-istochniki.json").write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    aggregated: dict[tuple[str, int, str], dict] = defaultdict(lambda: {"bad": 0, "total": 0, "not_periodic": 0, "vri_id": [], "owner_names": set()})
    for record in records:
        group = record["owner_group"]
        if group not in {"ink", "oemk", "apatit"}:
            continue
        key = (group, record["year"], record["model"])
        target = aggregated[key]
        if str(record["vriType"]) != "2":
            target["not_periodic"] += 1
            continue
        target["total"] += 1
        target["bad"] += int(not record["applicability"])
        target["vri_id"].append(record["vri_id"])
        target["owner_names"].add(record["miOwner"])
    output_rows = []
    for (group, year, model), values in sorted(aggregated.items()):
        output_rows.append({
            "owner_group": group,
            "year": year,
            "model": model,
            "bad": values["bad"],
            "total": values["total"],
            "not_periodic_excluded": values["not_periodic"],
            "owner_names": " | ".join(sorted(values["owner_names"])),
            "vri_id": " ".join(values["vri_id"]),
        })
    with (DATA / "vladeltsy.csv").open("w", encoding="utf-8-sig", newline="") as file:
        fields = ["owner_group", "year", "model", "bad", "total", "not_periodic_excluded", "owner_names", "vri_id"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)
    summary = {
        "sample_size": len(records),
        "periodic": sum(str(record["vriType"]) == "2" for record in records),
        "target_owner_cards": sum(record["owner_group"] in {"ink", "oemk", "apatit"} for record in records),
        "all_target_rows": output_rows,
        "owner_group_counts": dict(Counter(record["owner_group"] for record in records if record["owner_group"])),
        "limitation": "Случайная выборка внутри пяти крупнейших страт, а не все поверки владельца. Доли описательные; вывод о преимуществе модели по ним не делается.",
    }
    (DATA / "vladeltsy-svodka.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
