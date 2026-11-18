"""Import vacancies and apply status commands to the local registry on Windows."""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
import html
import io
import json
import msvcrt
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[4]
FIELDS = (
    "Internal_ID", "Source", "External_ID", "Vacancy_Title", "Employer",
    "Location", "Work_Format", "Employment_Type", "Salary_From", "Salary_To",
    "Salary_Currency", "Published_At", "Collected_At", "Vacancy_URL",
    "Record_Status", "Status_Changed_At",
)
REQUIRED = ("Source", "External_ID", "Vacancy_Title")
SERVICE = {"Internal_ID", "Collected_At", "Record_Status", "Status_Changed_At"}
CONTENT = tuple(field for field in FIELDS if field not in SERVICE)
SALARY = {"Salary_From", "Salary_To"}
STATUSES = ("ACTIVE", "EXCLUDED", "ARCHIVED")
RESULTS = ("ADDED", "DUPLICATE", "CONFLICT", "REJECTED")
ACTIONS_PATH = "input/record_actions.csv"
ACTION_FIELDS = ("Internal_ID", "Action", "Reason")
ACTION_TARGETS = {"EXCLUDE": "EXCLUDED", "ARCHIVE": "ARCHIVED", "RESTORE": "ACTIVE"}
REVIEW_FIELDS = (
    "Run_ID", "Input_File", "Record_Number", "Result", "Reason", "Source",
    "External_ID", "Record_JSON",
)
COMMAND_REVIEW_FIELDS = REVIEW_FIELDS + ("Internal_ID", "Action", "Command_Reason")
INTERNAL_ID = re.compile(r"VC-(?:[0-9]{6}|[1-9][0-9]{6,})", re.ASCII)
BOM = b"\xef\xbb\xbf"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
TIMESTAMP = re.compile(
    r"(?P<whole>(?:\d{4}-\d{2}-\d{2}|\d{8})T"
    r"(?:\d{2}:\d{2}(?::\d{2})?|\d{2}(?:\d{2}){0,2}))"
    r"(?:[.,](?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}(?::?\d{2})?)",
    re.ASCII,
)
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
CSV_RECORD = re.compile(r'(?:"(?:[^"]|"")*"|[^";\r\n]*)(?:;(?:"(?:[^"]|"")*"|[^";\r\n]*))*(?:\r\n|\r|\n|\Z)')


def utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def project_path(root, path):
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Путь выходит за пределы проекта: {path}")
    return resolved


@contextmanager
def registry_lock(path):
    # Windows permits locking a byte beyond EOF. Keep the same file across runs.
    with path.open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Не удалось получить исключительную блокировку реестра") from exc
        yield  # Closing the handle releases the lock even on error.


def formula_prefix(value):
    index = 0
    while index < len(value) and (value[index].isspace() or ord(value[index]) < 32):
        index += 1
    return value.startswith(("\t", "\r", "\n")) or value[index:index + 1] in {"=", "+", "-", "@"}


def escape_cell(value):
    if value.startswith("'") or formula_prefix(value):
        return "'" + value
    return value


def unescape_cell(value):
    if value.startswith("'") and escape_cell(value[1:]) == value:
        return value[1:]
    return value


def csv_rows(text):
    # csv.reader(strict=True) still accepts bare quotes in unquoted fields.
    position = 0
    while position < len(text):
        match = CSV_RECORD.match(text, position)
        if not match or match.end() == position:
            raise ValueError("Некорректное экранирование или структура CSV")
        position = match.end()
    return csv.reader(io.StringIO(text, newline=""), delimiter=";", strict=True)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющееся поле JSON: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"Недопустимое число JSON: {value}")


def read_input(path):
    text = path.read_bytes().decode("utf-8-sig")
    if path.suffix.lower() == ".json":
        batch = json.loads(text, parse_float=Decimal, object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
        if isinstance(batch, dict):
            batch = batch.get("items")
        if not isinstance(batch, list):
            raise ValueError("Ожидается массив JSON или объект с массивом items")
        return batch
    if path.suffix.lower() != ".csv":
        raise ValueError("Ожидается файл .json или .csv")
    return read_csv_batch(text)


def read_csv_batch(text, *, command_file=False):
    rows = csv_rows(text)
    header = next(rows, [])
    if command_file:
        valid_header = tuple(header) == ACTION_FIELDS
    else:
        valid_header = (header and all(header) and len(set(header)) == len(header)
                        and ";" not in "".join(header) and set(header).intersection(REQUIRED))
    if not valid_header:
        raise ValueError("Некорректный заголовок CSV")
    records = []
    for cells in rows:
        if len(cells) != len(header):
            raise ValueError(f"Неверное число полей CSV в записи {len(records) + 1}")
        records.append(dict(zip(header, cells, strict=True)))
    return records


def publication_value(value):
    if not value:
        return ""
    if DATE.fullmatch(value):
        return date.fromisoformat(value).isoformat()
    match = TIMESTAMP.fullmatch(value)
    if not match:
        raise ValueError("Published_At: ожидается дата или дата-время с часовым поясом")
    whole, fraction, zone = match.group("whole", "fraction", "zone")
    if fraction is not None and len(whole.split("T")[1].replace(":", "")) != 6:
        raise ValueError("Published_At: дробная часть указывается для секунд")
    offset = zone[1:].replace(":", "")
    if zone != "Z" and (int(offset[:2]) > 23 or (len(offset) == 4 and int(offset[2:]) > 59)):
        raise ValueError("Published_At: некорректное часовое смещение")
    instant = datetime.fromisoformat(whole + zone).astimezone(UTC)
    fraction = (fraction or "").rstrip("0")
    suffix = "." + fraction if fraction else ""
    return instant.isoformat(timespec="seconds").removesuffix("+00:00") + suffix + "Z"


def normalize(record):
    if not isinstance(record, dict):
        raise ValueError("Запись должна быть объектом")
    values = {}
    for field in CONTENT:
        value = record.get(field)
        if value is None:
            value = ""
        if field in SALARY and type(value) in (int, Decimal):
            value = str(value)
        if field == "External_ID" and type(value) is int:
            value = str(value)
        if not isinstance(value, str):
            raise ValueError(f"{field}: недопустимый тип значения")
        value = value.strip()
        if field in REQUIRED and not value:
            raise ValueError(f"{field}: отсутствует обязательное значение")
        value.encode("utf-8")  # Reject unpaired JSON surrogates at record level.
        if field in SALARY and value:
            if not NUMBER.fullmatch(value):
                raise ValueError(f"{field}: ожидается десятичное число")
            try:
                Decimal(value)
            except ArithmeticError as exc:
                raise ValueError(f"{field}: число вне допустимого диапазона") from exc
        if field == "Published_At":
            try:
                value = publication_value(value)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"Published_At: некорректная дата ({exc})") from exc
        values[field] = value
    return values


def key_of(row):
    return row["Source"], row["External_ID"]


def valid_internal_id(value):
    return bool(INTERNAL_ID.fullmatch(value)) and value != "VC-000000"


def signature(row):
    return tuple(Decimal(row[field]) if field in SALARY and row[field] else row[field]
                 for field in CONTENT)


def registry_rows(raw):
    if not raw.startswith(BOM):
        raise ValueError("Реестр должен содержать UTF-8 BOM")
    rows = csv_rows(raw.decode("utf-8-sig"))
    if tuple(next(rows, [])) != FIELDS:
        raise ValueError("Неверный заголовок реестра")
    stored = []
    index = {}
    ids = set()
    for cells in rows:
        row = dict(zip(FIELDS, cells, strict=True))
        decoded = {field: unescape_cell(value) for field, value in row.items()}
        if any(escape_cell(decoded[field]) != value for field, value in row.items()):
            raise ValueError("Некорректное защитное экранирование в реестре")
        normalized = normalize(decoded)
        key = key_of(normalized)
        identifier = decoded["Internal_ID"]
        if not valid_internal_id(identifier) or identifier in ids or key in index:
            raise ValueError("Некорректный или повторный ID либо ключ в реестре")
        if decoded["Record_Status"] not in STATUSES:
            raise ValueError("Недопустимый статус в реестре")
        for field in ("Collected_At", "Status_Changed_At"):
            stamp = decoded[field]
            if "T" not in stamp or not stamp.endswith("Z") or not publication_value(stamp):
                raise ValueError(f"Некорректная служебная отметка {field}")
        ids.add(identifier)
        stored.append(row)
        index[key] = signature(normalized)
    return stored, index


def snapshot(raw, rows):
    return {
        "Record_Count": len(rows),
        "By_Status": {status: sum(row["Record_Status"] == status for row in rows)
                      for status in STATUSES},
        "SHA256": sha256(raw).hexdigest() if raw is not None else None,
    }


def read_registry(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def serialize_csv(fields, rows, *, protect=True):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(fields)
    for row in rows:
        writer.writerow(escape_cell(row[field]) if protect else row[field] for field in fields)
    return BOM + buffer.getvalue().encode("utf-8")


def commit_registry(path, expected):
    payload = serialize_csv(FIELDS, expected, protect=False)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                     delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        staged_rows, _ = registry_rows(staged.read_bytes())
        if staged_rows != expected:
            raise ValueError("Проверка подготовленного реестра не пройдена")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return payload


def json_safe_text(value):
    return str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")


def review_entry(run_id, input_name, number, result, reason, record):
    original = record if isinstance(record, dict) else {}
    return {
        "Run_ID": run_id,
        "Input_File": input_name,
        "Record_Number": str(number),
        "Result": result,
        "Reason": reason,
        "Source": json_safe_text(original.get("Source", "")),
        "External_ID": json_safe_text(original.get("External_ID", "")),
        "Record_JSON": json_safe_text(json.dumps(record, ensure_ascii=False, default=str)),
    }


def plan_import(batch, index, stored, journal, review):
    groups = defaultdict(list)
    counts = journal["Counts"]

    def flag(number, result, reason, original):
        counts[result] += 1
        review.append(review_entry(journal["Run_ID"], journal["Input_File"],
                                   number, result, reason, original))

    for number, original in enumerate(batch, 1):
        try:
            normalized = normalize(original)
        except (ValueError, OverflowError) as exc:
            flag(number, "REJECTED", str(exc), original)
            continue
        groups[key_of(normalized)].append((number, original, normalized, signature(normalized)))

    pending = []
    for key, entries in groups.items():
        if key in index:
            for number, original, _, values in entries:
                if values == index[key]:
                    counts["DUPLICATE"] += 1
                else:
                    flag(number, "CONFLICT", "Содержательные поля отличаются от реестра", original)
        elif len({entry[3] for entry in entries}) > 1:
            for number, original, _, _ in entries:
                flag(number, "CONFLICT", "В пакете различаются записи с одним новым ключом", original)
        else:
            number, _, normalized, _ = entries[0]
            pending.append((number, normalized))
            counts["DUPLICATE"] += len(entries) - 1

    next_id = max((int(row["Internal_ID"][3:]) for row in stored), default=0) + 1
    stamp = utc_now()
    additions = [
        dict(row, Internal_ID=f"VC-{next_id + offset:06d}", Collected_At=stamp,
             Record_Status="ACTIVE", Status_Changed_At=stamp)
        for offset, (_, row) in enumerate(pending)
    ]
    return additions


def plan_management(batch, stored, journal, review):
    positions = {row["Internal_ID"]: position for position, row in enumerate(stored)}
    occurrences = Counter(row["Internal_ID"].strip() for row in batch)
    updated = stored.copy()
    changes = []
    for number, command in enumerate(batch, 1):
        identifier = command["Internal_ID"].strip()
        action = command["Action"].strip()
        reason = command["Reason"]
        target = ACTION_TARGETS.get(action)
        if occurrences[identifier] > 1:
            error = "Несколько команд для одного Internal_ID"
        elif not valid_internal_id(identifier):
            error = "Некорректный Internal_ID"
        elif identifier not in positions:
            error = "Неизвестный Internal_ID"
        elif target is None:
            error = "Недопустимая команда Action"
        elif action != "RESTORE" and not reason.strip():
            error = "Для этой команды требуется непустой Reason"
        else:
            error = None

        if error:
            journal["Counts"]["REJECTED"] += 1
            entry = review_entry(journal["Run_ID"], journal["Input_File"],
                                 number, "REJECTED", error, command)
            review.append(dict(entry, Internal_ID=command["Internal_ID"],
                               Action=command["Action"], Command_Reason=reason))
            continue

        position = positions[identifier]
        current = stored[position]["Record_Status"]
        event = {"Record_Number": number, "Internal_ID": identifier, "Action": action,
                 "Reason": reason, "Old_Status": current, "New_Status": target}
        if current == target:
            journal["Counts"]["UNCHANGED"] += 1
            journal["Unchanged_Commands"].append(event)
            continue
        stamp = utc_now()
        updated[position] = dict(stored[position], Record_Status=target, Status_Changed_At=stamp)
        changes.append(dict(event, Status_Changed_At=stamp))
    return updated, changes


def write_artifacts(root, journal, review):
    run_id = journal["Run_ID"]
    logs = project_path(root, "outputs/logs")
    reports = project_path(root, "outputs/reports")
    logs.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves earlier runs even in the event of a collision.
    with (logs / f"run_{run_id}.json").open("x", encoding="utf-8") as stream:
        json.dump(journal, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    lines = [
        f"# {journal['Mode']} — {run_id}", "", f"Итог — {journal['Outcome']}",
        f"Начало — {journal['Started_At']}",
        f"Вход — {html.escape(journal['Input_File'])}",
        f"Обработано — {journal['Processed']}", "",
        "| Результат | Количество |", "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in journal["Counts"].items())
    if journal["Mode"] == "MANAGE":
        for heading, events in (("Изменения статусов", journal["Status_Changes"]),
                                ("Команды без изменений", journal["Unchanged_Commands"])):
            if events:
                lines.extend(["", f"## {heading}", ""])
                for event in events:
                    lines.append(f"- {event['Internal_ID']} ({event['Action']}) — "
                                 f"{event['Old_Status']} → {event['New_Status']}")
    lines.extend(["", f"[Записи на уточнение](needs_review_{run_id}.csv)"])
    lines.extend(f"\n- {html.escape(error)}" for error in journal["Errors"])
    with (reports / f"run_{run_id}.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    with (reports / f"needs_review_{run_id}.csv").open("xb") as stream:
        fields = COMMAND_REVIEW_FIELDS if journal["Mode"] == "MANAGE" else REVIEW_FIELDS
        stream.write(serialize_csv(fields, sorted(review, key=lambda row: int(row["Record_Number"]))))


def run_import(input_path, root=ROOT):
    return run("IMPORT", input_path, root)


def run_manage(root=ROOT):
    return run("MANAGE", ACTIONS_PATH, root)


def run(mode, input_path, root):
    root = root.resolve(strict=True)
    journal = {
        "Run_ID": uuid4().hex,
        "Started_At": utc_now(),
        "Mode": mode,
        "Input_File": json_safe_text(input_path),
        "Outcome": "ERROR",
        "Processed": 0,
        "Counts": dict.fromkeys(RESULTS, 0),
        "Status_Changes": [],
        "Registry_Before": None,
        "Registry_After": None,
        "Errors": [],
    }
    if mode == "MANAGE":
        journal["Counts"].update(CHANGED=0, UNCHANGED=0)
        journal["Unchanged_Commands"] = []
    review = []
    try:
        registry = project_path(root, "data/vacancies_registry.csv")
        lock = project_path(root, "data/vacancies_registry.lock")
        with registry_lock(lock):
            raw = read_registry(registry)
            unavailable = {"Record_Count": None, "By_Status": None,
                           "SHA256": sha256(raw).hexdigest() if raw is not None else None}
            journal["Registry_Before"] = journal["Registry_After"] = unavailable
            stored, index = registry_rows(raw) if raw is not None else ([], {})
            before = snapshot(raw, stored)
            journal["Registry_Before"] = journal["Registry_After"] = before
            source = project_path(root, input_path)
            if not source.is_relative_to(root / "input"):
                raise ValueError("Входной файл должен находиться внутри input/")
            journal["Input_File"] = source.relative_to(root).as_posix()
            if mode == "MANAGE":
                batch = read_csv_batch(source.read_bytes().decode("utf-8-sig"), command_file=True)
                updated, changes = plan_management(batch, stored, journal, review)
                added = 0
            else:
                batch = read_input(source)
                additions = plan_import(batch, index, stored, journal, review)
                encoded = [{field: escape_cell(row[field]) for field in FIELDS} for row in additions]
                updated = stored + encoded
                added = len(additions)
                changes = []
            journal["Processed"] = len(batch)
            if added or changes:
                payload = commit_registry(registry, updated)
                journal["Counts"]["ADDED"] = added
                if changes:
                    journal["Counts"]["CHANGED"] = len(changes)
                    journal["Status_Changes"] = changes
                journal["Registry_After"] = None
                actual = registry.read_bytes()
                rows, _ = registry_rows(actual)
                journal["Registry_After"] = snapshot(actual, rows)
                if actual != payload:
                    raise ValueError("Реестр после записи отличается от проверенного файла")
            journal["Outcome"] = "OK"
    except (OSError, ValueError, RuntimeError, OverflowError, csv.Error) as exc:
        journal["Errors"].append(json_safe_text(exc))
    write_artifacts(root, journal, review)
    return journal


def main():
    parser = argparse.ArgumentParser(description="Локальный реестр вакансий")
    parser.add_argument("mode", choices=["IMPORT", "MANAGE"])
    parser.add_argument("input_file", nargs="?", help="Путь к JSON/CSV для IMPORT внутри input/")
    args = parser.parse_args()
    if args.mode == "IMPORT" and args.input_file is None:
        parser.error("IMPORT требует путь к входному файлу")
    if args.mode == "MANAGE" and args.input_file is not None:
        parser.error(f"MANAGE читает {ACTIONS_PATH} и не принимает путь")
    try:
        journal = run_import(args.input_file) if args.mode == "IMPORT" else run_manage()
    except (OSError, ValueError) as exc:
        print(f"Ошибка записи артефактов запуска: {exc}", file=sys.stderr)
        return 1
    print(f"{journal['Run_ID']} {journal['Outcome']}")
    return 0 if journal["Outcome"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
