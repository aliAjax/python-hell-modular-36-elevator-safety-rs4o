import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("inspector", "inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(
            self.admin, "equipment",
            {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )

    def _passed_inspection(self, equipment_id):
        inspection = self.service.create(
            self.admin, "inspection",
            {"equipment_id": equipment_id, "scheduled_at": "2026-09-01T00:00:00Z", "cycle_days": 365},
        )
        return self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})

    def _closed_remediation(self, equipment_id):
        remediation = self.service.create(
            self.admin, "remediation",
            {"equipment_id": equipment_id, "issue": "x", "owner": "M", "due_at": "2026-10-01"},
        )
        remediation = self.service.transition(self.admin, remediation["id"], "submit_evidence", {"evidence": "IMG"})
        remediation = self.service.transition(self.admin, remediation["id"], "verify", {})
        return self.service.transition(self.admin, remediation["id"], "close", {})

    def _granted_permit(self, equipment_id):
        permit = self.service.create(
            self.admin, "permit",
            {"equipment_id": equipment_id, "purpose": "return_to_service", "requested_by": "ops"},
        )
        self.service.transition(self.admin, permit["id"], "request_review", {})
        return self.service.transition(self.admin, permit["id"], "grant", {})

    def _active_alarm(self, equipment_id, code="DOOR"):
        return self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment_id, "code": code, "occurred_at": "2026-10-01T00:00:00Z"},
        )

    def _close_alarm(self, alarm):
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "A"})
        job = self.service.create(self.admin, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j1", "team": "A"})
        self.service.transition(self.admin, job["id"], "arrive", {})
        self.service.transition(self.admin, job["id"], "complete", {"outcome": "ok"})
        self.service.transition(self.admin, alarm["id"], "resolve", {"resolution": "ok"})
        return self.service.transition(self.admin, alarm["id"], "close", {})

    def test_merge_creates_and_applies_inspection_result(self):
        equipment = self.equipment()
        record = {
            "source_id": "dev1", "record_id": "insp-1", "kind": "inspection", "version": 1,
            "data": {"equipment_id": equipment["id"], "scheduled_at": "2026-09-10T00:00:00Z", "cycle_days": 365},
            "action": "pass", "action_data": {"findings": "offline ok"},
        }
        result = self.service.merge_offline(self.inspector, [record])[0]
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["entity"]["status"], "passed")

    def test_merge_applies_maintenance_lifecycle(self):
        equipment = self.equipment()
        record = {
            "source_id": "dev1", "record_id": "maint-1", "kind": "maintenance", "version": 1,
            "data": {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-10T00:00:00Z"},
            "actions": ["start", "complete"], "action_data": {"completed_at": "2026-09-11T00:00:00Z"},
        }
        result = self.service.merge_offline(self.inspector, [record])[0]
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["entity"]["status"], "completed")

    def test_same_source_record_processed_once(self):
        equipment = self.equipment()
        record = {
            "source_id": "dev1", "record_id": "insp-1", "kind": "inspection", "version": 1,
            "data": {"equipment_id": equipment["id"], "scheduled_at": "2026-09-10T00:00:00Z", "cycle_days": 365},
            "action": "pass", "action_data": {"findings": "ok"},
        }
        first = self.service.merge_offline(self.inspector, [record])[0]
        second = self.service.merge_offline(self.inspector, [record])[0]
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["entity"]["id"], first["entity"]["id"])

    def test_stale_version_keeps_center_and_lists_diffs(self):
        equipment = self.equipment()
        maintenance = self.service.create(
            self.admin, "maintenance",
            {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-09-10T00:00:00Z"},
        )
        self.service.transition(self.admin, maintenance["id"], "start", {})
        maintenance = self.service.get(maintenance["id"])
        self.assertEqual(maintenance["version"], 2)

        stale = {
            "source_id": "dev1", "record_id": "maint-stale", "kind": "maintenance", "version": 1,
            "entity_id": maintenance["id"],
            "data": {"equipment_id": equipment["id"], "work_type": "repair", "planned_at": "2026-09-10T00:00:00Z"},
            "actions": ["start", "complete"], "action_data": {"completed_at": "2026-09-11T00:00:00Z"},
        }
        result = self.service.merge_offline(self.inspector, [stale])[0]
        self.assertEqual(result["status"], "conflict")
        fields = {d["field"] for d in result["diffs"]}
        self.assertIn("work_type", fields)
        center = self.service.get(maintenance["id"])
        self.assertEqual(center["version"], 2)
        self.assertEqual(center["data"]["work_type"], "routine")
        self.assertEqual(center["status"], "in_progress")

    def test_result_held_when_equipment_has_active_alarm(self):
        equipment = self.equipment()
        self._active_alarm(equipment["id"])
        record = {
            "source_id": "dev1", "record_id": "maint-1", "kind": "maintenance", "version": 1,
            "data": {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-02T00:00:00Z"},
            "actions": ["start", "complete"], "action_data": {"completed_at": "2026-10-03T00:00:00Z"},
        }
        result = self.service.merge_offline(self.inspector, [record])[0]
        self.assertEqual(result["status"], "held")
        self.assertTrue(result["entity"]["data"]["held"])
        self.assertIn("unclosed alarm", result["entity"]["data"]["hold_reason"])
        self.assertEqual(result["entity"]["status"], "in_progress")

    def test_result_applied_after_alarm_closes(self):
        equipment = self.equipment()
        alarm = self._active_alarm(equipment["id"])
        self._close_alarm(alarm)
        record = {
            "source_id": "dev1", "record_id": "maint-1", "kind": "maintenance", "version": 1,
            "data": {"equipment_id": equipment["id"], "work_type": "routine", "planned_at": "2026-10-02T00:00:00Z"},
            "actions": ["start", "complete"], "action_data": {"completed_at": "2026-10-03T00:00:00Z"},
        }
        result = self.service.merge_offline(self.inspector, [record])[0]
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["entity"]["status"], "completed")

    def test_equipment_status_change_returns_granted_permit_to_review(self):
        equipment = self.equipment()
        self._passed_inspection(equipment["id"])
        self._closed_remediation(equipment["id"])
        permit = self._granted_permit(equipment["id"])
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")

        self.service.transition(self.admin, equipment["id"], "suspend", {})
        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "pending_review")
        self.assertIn("suspended", permit["data"]["reason"])

    def test_equipment_status_change_keeps_pending_permit_in_review(self):
        equipment = self.equipment()
        self._passed_inspection(equipment["id"])
        self._closed_remediation(equipment["id"])
        permit = self.service.create(
            self.admin, "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        self.service.transition(self.admin, permit["id"], "request_review", {})
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")

        self.service.transition(self.admin, equipment["id"], "suspend", {})
        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "pending_review")


if __name__ == "__main__":
    unittest.main()
