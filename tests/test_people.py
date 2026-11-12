import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import build
from replay import people


class PeopleMappingTests(unittest.TestCase):
    def write_people(self, path, entries):
        path.write_text(json.dumps({"people": entries}), encoding="utf-8")

    def person(self, pid, name, github=None, job_title=None):
        return {
            "id": pid,
            "name": name,
            "short": name,
            "kind": "human",
            "color": "hsl(200 55% 68%)",
            "slack": [],
            "femini": [],
            "github": github or [],
            "hubspot": [],
            **({"job_title": job_title} if job_title else {}),
        }

    def test_account_alias_matching_ignores_case_and_repeated_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "people.json"
            self.write_people(path, [self.person("alice", "Alice", ["Alice Smith"])])
            with patch.object(people, "_FILE", path):
                roster = people.Roster()
                self.assertEqual(roster.resolve("github", "  ALICE   SMITH "), "alice")
                self.assertEqual(roster.used, ["alice"])

    def test_conflicting_aliases_are_rejected_instead_of_silently_merging(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "people.json"
            self.write_people(path, [
                self.person("alice", "Alice", ["same-login"]),
                self.person("bob", "Bob", [" SAME-LOGIN "]),
            ])
            with patch.object(people, "_FILE", path):
                with self.assertRaisesRegex(ValueError, "mapped to multiple people"):
                    people.Roster()

    def test_campaign_supports_multiple_people_and_multiple_responsibilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            people_path, roles_path = root / "people.json", root / "campaign_roles.json"
            self.write_people(people_path, [
                self.person("alice", "Alice", job_title={"zh": "项目经理", "en": "Project Manager"}),
                self.person("bob", "Bob"),
            ])
            roles_path.write_text(json.dumps({"campaigns": {"77": [
                {"person_id": "alice", "role_zh": "PM", "role_en": "PM"},
                {"person_id": "alice", "role_zh": "统筹", "role_en": "Coordinator"},
                {"person_id": "bob", "role_zh": "开发", "role_en": "Developer"},
            ]}}), encoding="utf-8")
            with patch.object(people, "_FILE", people_path), patch.object(people, "_ROLES_FILE", roles_path):
                roster = people.Roster()
                roster.touch("alice")
                roster.touch("bob")
                assigned = roster.campaign_roles(77)
                zh = build._people({"roster": roster}, [{"actor": "alice", "kind": "chat"}], "zh", assigned)
                en = build._people({"roster": roster}, [{"actor": "alice", "kind": "chat"}], "en", assigned)
            self.assertEqual(zh[0]["job_title"], "项目经理")
            self.assertEqual(zh[0]["responsibilities"], ["PM", "统筹"])
            self.assertEqual(en[0]["job_title"], "Project Manager")
            self.assertEqual(en[0]["responsibilities"], ["PM", "Coordinator"])
            for item in zh + en:
                self.assertNotIn("accounts", item)
                self.assertNotIn("evidence", item)
                self.assertNotIn("conf", item)

    def test_campaign_roles_reject_bots_and_bot_job_titles_are_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            people_path, roles_path = root / "people.json", root / "campaign_roles.json"
            bot = self.person("bot", "FeedBot", job_title={"zh": "管理员", "en": "Admin"})
            bot["kind"] = "bot"
            self.write_people(people_path, [bot])
            with patch.object(people, "_FILE", people_path), patch.object(people, "_ROLES_FILE", roles_path):
                roster = people.Roster()
                roster.touch("bot")
                exported = build._people({"roster": roster}, [], "zh")
                self.assertIsNone(exported[0]["job_title"])
                roles_path.write_text(json.dumps({"campaigns": {"77": [
                    {"person_id": "bot", "role_zh": "项目经理", "role_en": "Project Manager"}
                ]}}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "only be assigned to colleagues"):
                    roster.campaign_roles(77)

    def test_campaign_role_requires_known_person_and_bilingual_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            people_path, roles_path = root / "people.json", root / "campaign_roles.json"
            self.write_people(people_path, [self.person("alice", "Alice")])
            with patch.object(people, "_FILE", people_path), patch.object(people, "_ROLES_FILE", roles_path):
                roster = people.Roster()
                roles_path.write_text(json.dumps({"campaigns": {"77": [
                    {"person_id": "unknown", "role_zh": "开发", "role_en": "Developer"}
                ]}}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Unknown person"):
                    roster.campaign_roles(77)
                roles_path.write_text(json.dumps({"campaigns": {"77": [
                    {"person_id": "alice", "role_zh": "PM", "role_en": ""}
                ]}}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "requires Chinese and English"):
                    roster.campaign_roles(77)


if __name__ == "__main__":
    unittest.main()
