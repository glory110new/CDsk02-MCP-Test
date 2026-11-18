import csv
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = Path(".agents/skills/vacancy-collector/scripts/import_vacancies.py")
FIXTURES = PROJECT / "tests/fixtures"
CORE = runpy.run_path(str(PROJECT / SCRIPT))
REGISTRY = Path("data/vacancies_registry.csv")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT / "tests" / f"scratch-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(self.remove_scratch)
        for relative in ("data", "input", "outputs/logs", "outputs/reports", SCRIPT.parent):
            (self.root / relative).mkdir(parents=True)
        shutil.copyfile(PROJECT / SCRIPT, self.root / SCRIPT)
        for fixture in FIXTURES.iterdir():
            shutil.copyfile(fixture, self.root / "input" / fixture.name)

    def remove_scratch(self):
        for relative in ("data", "input", "outputs/logs", "outputs/reports", SCRIPT.parent):
            directory = self.root / relative
            if directory.is_dir():
                for entry in directory.iterdir():
                    entry.unlink()
                directory.rmdir()
        for relative in ("outputs", SCRIPT.parent.parent, ".agents/skills", ".agents"):
            directory = self.root / relative
            if directory.is_dir():
                directory.rmdir()
        self.root.rmdir()

    def launch(self, filename=None, outcome="OK", *, mode="IMPORT"):
        command = [sys.executable, "-B", str(self.root / SCRIPT), mode]
        if filename is not None:
            command.append(f"input/{filename}")
        result = subprocess.run(
            command,
            cwd=self.root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, int(outcome != "OK"), result.stderr)
        run_id, observed = result.stdout.split()
        self.assertEqual(observed, outcome)
        log = json.loads((self.root / f"outputs/logs/run_{run_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(log["Run_ID"], run_id)
        self.assertEqual(log["Outcome"], outcome)
        self.assertEqual(log["Mode"], mode)
        self.assertTrue(log["Started_At"].endswith("Z"))
        report = (self.root / f"outputs/reports/run_{run_id}.md").read_text(encoding="utf-8")
        self.assertIn(run_id, report)
        self.assertIn(f"# {mode} —", report)
        fields = CORE["COMMAND_REVIEW_FIELDS"] if mode == "MANAGE" else CORE["REVIEW_FIELDS"]
        review = self.read_csv(Path(f"outputs/reports/needs_review_{run_id}.csv"), fields)
        self.assertTrue(all(row["Run_ID"] == run_id for row in review))
        return log, review

    def read_csv(self, path, fields):
        raw = (self.root / path).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";")
        self.assertEqual(tuple(reader.fieldnames), fields)
        return list(reader)

    def test_initial_repeat_and_mixed(self):
        initial, review = self.launch("initial.json")
        self.assertFalse(review)
        self.assertEqual(initial["Counts"], dict(ADDED=10, DUPLICATE=0, CONFLICT=0, REJECTED=0))
        registry = self.root / REGISTRY
        raw = registry.read_bytes()
        modified = registry.stat().st_mtime_ns
        rows = self.read_csv(REGISTRY, CORE["FIELDS"])
        self.assertEqual([row["Internal_ID"] for row in rows], [f"VC-{n:06d}" for n in range(1, 11)])
        self.assertEqual(rows[0]["External_ID"], "001")
        self.assertEqual(rows[0]["Vacancy_Title"], "'=Analyst")
        self.assertEqual(rows[1]["Vacancy_Title"], "''Analyst")
        self.assertEqual(rows[0]["Salary_From"], "'-1000.00")
        self.assertEqual(rows[0]["Published_At"], "2026-11-13T09:00:00Z")
        self.assertEqual(rows[1]["Published_At"], "2026-11-13")
        self.assertEqual(rows[2]["Published_At"], "")
        self.assertTrue(all(row["Collected_At"].endswith("Z") and
                            row["Status_Changed_At"] == row["Collected_At"] for row in rows))
        self.assertEqual(initial["Registry_After"]["SHA256"], sha256(raw).hexdigest())
        self.assertEqual(initial["Registry_After"]["By_Status"], dict(ACTIVE=10, EXCLUDED=0, ARCHIVED=0))

        repeat, _ = self.launch("initial.json")
        self.assertNotEqual(initial["Run_ID"], repeat["Run_ID"])
        self.assertEqual(repeat["Counts"]["DUPLICATE"], 10)
        self.assertEqual(repeat["Registry_Before"], repeat["Registry_After"])
        self.assertEqual(registry.read_bytes(), raw)
        self.assertEqual(registry.stat().st_mtime_ns, modified)

        mixed, _ = self.launch("mixed.csv")
        self.assertEqual(mixed["Counts"], dict(ADDED=5, DUPLICATE=3, CONFLICT=0, REJECTED=0))
        after = self.read_csv(REGISTRY, CORE["FIELDS"])
        self.assertEqual(rows, after[:10])
        self.assertEqual(after[-1]["Internal_ID"], "VC-000015")
        self.assertEqual(mixed["Registry_After"]["SHA256"], sha256(registry.read_bytes()).hexdigest())

    def test_review_and_valid_neighbour(self):
        self.launch("initial.json")
        before = self.read_csv(REGISTRY, CORE["FIELDS"])
        journal, review = self.launch("review.json")
        self.assertEqual(journal["Counts"], dict(ADDED=1, DUPLICATE=0, CONFLICT=3, REJECTED=3))
        self.assertEqual([row["Record_Number"] for row in review], list("123456"))
        self.assertIn("External_ID", review[0]["Reason"])
        self.assertEqual(review[1]["Result"], "CONFLICT")
        self.assertTrue(all("Published_At" in review[n]["Reason"] for n in (2, 3)))
        after = self.read_csv(REGISTRY, CORE["FIELDS"])
        self.assertEqual(before, after[:-1])
        self.assertEqual(after[-1]["External_ID"], "019")
        self.assertEqual(after[-1]["Published_At"], "2026-11-13T09:00:00.123456789Z")

    def test_malformed_input_preserves_registry(self):
        self.launch("initial.json")
        registry = self.root / REGISTRY
        before = registry.read_bytes()
        for filename in ("broken.json", "broken.csv"):
            with self.subTest(filename=filename):
                journal, _ = self.launch(filename, "ERROR")
                self.assertTrue(journal["Errors"])
                self.assertEqual(journal["Registry_Before"], journal["Registry_After"])
                self.assertEqual(registry.read_bytes(), before)

    def test_damaged_registry_is_preserved(self):
        registry = self.root / REGISTRY
        registry.write_bytes(b"\xef\xbb\xbfbroken")
        journal, _ = self.launch("initial.json", "ERROR")
        self.assertEqual(registry.read_bytes(), b"\xef\xbb\xbfbroken")
        self.assertIsNone(journal["Registry_Before"]["Record_Count"])
        self.assertEqual(journal["Registry_Before"]["SHA256"], sha256(registry.read_bytes()).hexdigest())

    def test_atomic_replace_failure_preserves_registry(self):
        self.launch("initial.json")
        registry = self.root / REGISTRY
        before = registry.read_bytes()
        with patch.object(os, "replace", side_effect=PermissionError("simulated replace failure")):
            journal = CORE["run_import"]("input/mixed.csv", root=self.root)
        self.assertEqual(journal["Outcome"], "ERROR")
        self.assertEqual(journal["Counts"]["ADDED"], 0)
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse(list(registry.parent.glob("*.tmp")))

    def test_lock_is_exclusive_and_recovers_after_process_death(self):
        lock = self.root / "data/vacancies_registry.lock"
        code = (
            "import msvcrt,sys,time; "
            "f=open(sys.argv[1], 'a+b'); f.seek(0); "
            "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1); "
            "print('ready', flush=True); time.sleep(30)"
        )
        with subprocess.Popen([sys.executable, "-B", "-c", code, str(lock)],
                              cwd=self.root, stdout=subprocess.PIPE, text=True) as holder:
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                journal, _ = self.launch("initial.json", "ERROR")
                self.assertTrue(journal["Errors"])
                self.assertFalse((self.root / REGISTRY).exists())
            finally:
                holder.kill()
        self.assertTrue(lock.is_file())
        journal, _ = self.launch("initial.json")
        self.assertEqual(journal["Counts"]["ADDED"], 10)

    def test_formula_round_trip(self):
        for value in ("=a", "+a", "-1", "@a", " \t=a", "\tdata", "\rdata", "\ndata", "'x", "''x", "001"):
            with self.subTest(value=value):
                encoded = CORE["escape_cell"](value)
                self.assertEqual(CORE["unescape_cell"](encoded), value)
                if value != "001":
                    self.assertTrue(encoded.startswith("'"))

    def test_csv_bom_and_multiline_cells(self):
        csv_path = self.root / "input/quoted.csv"
        csv_path.write_bytes(
            b'\xef\xbb\xbfSource;External_ID;Vacancy_Title\r\n'
            b'demo;007;"Part one;\r\nPart ""two"""\r\n'
        )
        journal, _ = self.launch("quoted.csv")
        self.assertEqual(journal["Counts"]["ADDED"], 1)
        self.assertEqual(self.read_csv(REGISTRY, CORE["FIELDS"])[0]["Vacancy_Title"],
                         'Part one;\r\nPart "two"')



    def set_commands(self, *rows, bom=False):
        with (self.root / "input/record_actions.csv").open(
            "w", encoding="utf-8-sig" if bom else "utf-8", newline=""
        ) as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(("Internal_ID", "Action", "Reason"))
            writer.writerows(rows)

    def test_manage_status_transitions_and_hashes(self):
        self.launch("initial.json")
        registry = self.root / REGISTRY
        for action, target, reason in (
            ("EXCLUDE", "EXCLUDED", "Excluded for review"),
            ("ARCHIVE", "ARCHIVED", "Archived by user"),
            ("RESTORE", "ACTIVE", ""),
        ):
            with self.subTest(action=action):
                self.set_commands(("VC-000001", action, reason), bom=action == "EXCLUDE")
                before = self.read_csv(REGISTRY, CORE["FIELDS"])
                digest = sha256(registry.read_bytes()).hexdigest()
                journal, review = self.launch(mode="MANAGE")
                after = self.read_csv(REGISTRY, CORE["FIELDS"])
                changed = after[0]
                self.assertFalse(review)
                self.assertEqual(journal["Counts"]["CHANGED"], 1)
                self.assertEqual(journal["Registry_Before"]["SHA256"], digest)
                self.assertEqual(journal["Registry_After"]["SHA256"], sha256(registry.read_bytes()).hexdigest())
                self.assertEqual(journal["Registry_After"]["Record_Count"], 10)
                self.assertGreater(journal["Registry_After"]["By_Status"][target], 0)
                self.assertEqual(changed["Record_Status"], target)
                self.assertNotEqual(changed["Status_Changed_At"], before[0]["Status_Changed_At"])
                self.assertTrue(changed["Status_Changed_At"].endswith("Z"))
                self.assertEqual(after[1:], before[1:])
                preserved = dict(changed, Record_Status=before[0]["Record_Status"],
                                 Status_Changed_At=before[0]["Status_Changed_At"])
                self.assertEqual(preserved, before[0])
                self.assertEqual(journal["Status_Changes"], [{
                    "Record_Number": 1, "Internal_ID": "VC-000001", "Action": action,
                    "Reason": reason, "Old_Status": before[0]["Record_Status"], "New_Status": target,
                    "Status_Changed_At": changed["Status_Changed_At"],
                }])

    def test_manage_repeated_command_preserves_bytes_and_timestamp(self):
        self.launch("initial.json")
        self.set_commands(("VC-000001", "EXCLUDE", "Excluded by user"))
        first, _ = self.launch(mode="MANAGE")
        registry = self.root / REGISTRY
        before = registry.read_bytes()
        modified = registry.stat().st_mtime_ns
        second, _ = self.launch(mode="MANAGE")
        self.assertNotEqual(first["Run_ID"], second["Run_ID"])
        self.assertEqual(second["Counts"]["CHANGED"], 0)
        self.assertEqual(second["Counts"]["UNCHANGED"], 1)
        self.assertEqual(second["Unchanged_Commands"][0]["Old_Status"], "EXCLUDED")
        self.assertEqual(second["Unchanged_Commands"][0]["New_Status"], "EXCLUDED")
        self.assertFalse(second["Status_Changes"])
        self.assertEqual(second["Registry_Before"], second["Registry_After"])
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(registry.stat().st_mtime_ns, modified)

    def test_manage_invalid_and_duplicate_commands_leave_valid_neighbour(self):
        self.launch("initial.json")
        self.set_commands(
            ("VC-999999", "EXCLUDE", "Unknown"),
            ("VC-000001", "=INVALID", "Invalid action"),
            ("VC-000002", "EXCLUDE", "   "),
            ("VC-000003", "ARCHIVE", ""),
            ("VC-000004", "EXCLUDE", "First"),
            (" VC-000004 ", "ARCHIVE", "Second"),
            ("bad-id", "RESTORE", ""),
            ("VC-000005", "EXCLUDE", "Valid neighbour"),
        )
        journal, review = self.launch(mode="MANAGE")
        self.assertEqual(journal["Processed"], 8)
        self.assertEqual(journal["Counts"]["REJECTED"], 7)
        self.assertEqual(journal["Counts"]["CHANGED"], 1)
        self.assertEqual([row["Record_Number"] for row in review], list("1234567"))
        self.assertEqual(review[1]["Action"], "'=INVALID")
        for row, fragment in zip(review, ("Неизвестный", "Action", "Reason", "Reason",
                                          "Несколько", "Несколько", "Некорректный"), strict=True):
            self.assertIn(fragment, row["Reason"])
            self.assertEqual(row["Result"], "REJECTED")
        rows = self.read_csv(REGISTRY, CORE["FIELDS"])
        self.assertEqual([row["Record_Status"] for row in rows[:5]],
                         ["ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE", "EXCLUDED"])

    def test_manage_malformed_file_preserves_registry(self):
        self.launch("initial.json")
        registry = self.root / REGISTRY
        before = registry.read_bytes()
        path = self.root / "input/record_actions.csv"
        valid_prefix = b"Internal_ID;Action;Reason\nVC-000001;EXCLUDE;Valid first row\n"
        for raw in (
            valid_prefix + b'VC-000002;ARCHIVE;"unclosed',
            valid_prefix + b"VC-000002;ARCHIVE",
            b"Internal_ID;Action\nVC-000001;EXCLUDE",
            valid_prefix + b"\xff",
        ):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                journal, review = self.launch(outcome="ERROR", mode="MANAGE")
                self.assertTrue(journal["Errors"])
                self.assertFalse(journal["Status_Changes"])
                self.assertFalse(review)
                self.assertEqual(journal["Registry_Before"], journal["Registry_After"])
                self.assertEqual(registry.read_bytes(), before)

    def test_manage_replace_failure_preserves_registry(self):
        self.launch("initial.json")
        self.set_commands(("VC-000001", "ARCHIVE", "Archive"))
        registry = self.root / REGISTRY
        before = registry.read_bytes()
        with patch.object(os, "replace", side_effect=PermissionError("simulated replace failure")):
            journal = CORE["run_manage"](root=self.root)
        self.assertEqual(journal["Outcome"], "ERROR")
        self.assertEqual(journal["Counts"]["CHANGED"], 0)
        self.assertFalse(journal["Status_Changes"])
        self.assertTrue(journal["Errors"])
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse(list(registry.parent.glob("*.tmp")))

    def test_import_after_manage_preserves_excluded_record(self):
        self.launch("initial.json")
        self.set_commands(("VC-000001", "EXCLUDE", "Keep excluded on repeat"))
        self.launch(mode="MANAGE")
        registry = self.root / REGISTRY
        before = registry.read_bytes()
        excluded = self.read_csv(REGISTRY, CORE["FIELDS"])[0]
        self.assertEqual(excluded["Record_Status"], "EXCLUDED")
        repeat, _ = self.launch("initial.json")
        self.assertEqual(repeat["Counts"]["DUPLICATE"], 10)
        self.assertEqual(registry.read_bytes(), before)
        mixed, _ = self.launch("mixed.csv")
        self.assertEqual(mixed["Counts"]["ADDED"], 5)
        self.assertEqual(mixed["Counts"]["DUPLICATE"], 3)
        after = self.read_csv(REGISTRY, CORE["FIELDS"])
        self.assertEqual(after[0], excluded)
        self.assertEqual(after[-1]["Internal_ID"], "VC-000015")


if __name__ == "__main__":
    unittest.main()
