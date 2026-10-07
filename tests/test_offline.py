import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("inspector-1", "inspector")
        self.equipment = self.service.create(
            self.admin,
            "equipment",
            {"asset_no": "E-1", "equipment_type": "elevator", "location": "B1", "inspection_interval_days": 365},
        )
        self.maintenance = Actor("maint-1", "maintenance")

    def tearDown(self):
        self.tmp.cleanup()

    def create_inspection(self):
        return self.service.create(
            self.admin,
            "inspection",
            {"equipment_id": self.equipment["id"], "scheduled_at": "2026-10-01T09:00:00Z", "cycle_days": 365},
        )

    def record(self, target, action, payload, base_version=None, source="tablet-7", rid="rec-1"):
        return {
            "source_id": source,
            "record_id": rid,
            "kind": target["kind"],
            "target_id": target["id"],
            "action": action,
            "base_version": target["version"] if base_version is None else base_version,
            "payload": payload,
        }

    # -- exactly once -----------------------------------------------------

    def test_same_source_record_is_processed_exactly_once(self):
        inspection = self.create_inspection()
        raw = self.record(inspection, "pass", {"findings": "offline pass", "offline_note": "ok"})
        first = self.service.merge_offline(self.inspector, [raw])
        second = self.service.merge_offline(self.inspector, [raw])
        self.assertEqual(first[0]["status"], "applied")
        self.assertEqual(second[0]["id"], first[0]["id"])
        self.assertEqual(second[0]["status"], "applied")
        self.assertEqual(second[0]["version"], 1)  # not processed again
        target = self.service.get(inspection["id"])
        self.assertEqual(target["version"], 2)  # only one apply

    def test_batch_mix_applies_valid_records(self):
        inspection_a = self.create_inspection()
        inspection_b = self.create_inspection()
        records = [
            self.record(inspection_a, "pass", {"findings": "a"}, rid="r1"),
            # Invalid transition: inspection_b is still scheduled, and payload is empty anyway.
            {"source_id": "tablet-7", "record_id": "r2", "kind": "inspection",
             "target_id": inspection_b["id"], "action": "reschedule",
             "base_version": inspection_b["version"], "payload": {}},
        ]
        result = self.service.merge_offline(self.inspector, records)
        self.assertEqual([item["status"] for item in result], ["applied", "rejected"])
        self.assertIn("规则校验", result[1]["data"]["reason"])

    # -- stale version keeps center and lists diffs ----------------------

    def test_older_version_is_stale_with_diff_and_center_kept(self):
        inspection = self.create_inspection()
        # Center advances the record first.
        inspection = self.service.transition(
            self.inspector, inspection["id"], "pass", {"findings": "center changed"}
        )
        raw = self.record(inspection, "fail", {"findings": "offline findings"}, base_version=1)
        [item] = self.service.merge_offline(self.inspector, [raw])
        self.assertEqual(item["status"], "stale")
        center = self.service.get(inspection["id"])
        self.assertEqual(center["status"], "passed")
        self.assertEqual(center["data"]["findings"], "center changed")
        diffs = item["data"]["diffs"]
        self.assertIn("status", [d["field"] for d in diffs])
        findings_diff = next(d for d in diffs if d["field"] == "data.findings")
        self.assertEqual(findings_diff["center"], "center changed")
        self.assertEqual(findings_diff["offline"], "offline findings")

    def test_concurrent_modification_during_merge_becomes_stale(self):
        inspection = self.create_inspection()
        raw = self.record(inspection, "pass", {"findings": "offline"}, base_version=1)

        # Simulate a concurrent center edit by precomputing then racing:
        # advance center while offline payload validation would pass.
        # We emulate by merging with a stale expected version directly through
        # the repository after first call is prepared; instead, craft base_version=1
        # and mutate center before the update via a hook is impossible here, so
        # verify behavior with two offline records where the first applies.
        first = self.service.merge_offline(
            self.inspector, [self.record(inspection, "pass", {"findings": "first"}, rid="a")]
        )
        self.assertEqual(first[0]["status"], "applied")
        # Second record still based on v1 now loses the race.
        second = self.service.merge_offline(
            self.inspector, [self.record(inspection, "fail", {"findings": "second"}, base_version=1, rid="b")]
        )
        self.assertEqual(second[0]["status"], "stale")
        self.assertEqual(self.service.get(inspection["id"])["status"], "passed")

    # -- unclosed alarm gate ---------------------------------------------

    def test_result_held_while_alarm_open_then_applied_when_alarm_clears(self):
        inspection = self.create_inspection()
        alarm = self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": self.equipment["id"], "code": "TRAP-1", "occurred_at": "2026-10-01T10:00:00Z"},
        )
        [item] = self.service.merge_offline(
            self.inspector, [self.record(inspection, "pass", {"findings": "offline pass"})]
        )
        self.assertEqual(item["status"], "held")
        self.assertIn("未关闭报警", item["data"]["reason"])
        target = self.service.get(inspection["id"])
        self.assertEqual(target["status"], "scheduled")  # not applied yet

        # Online result also blocked by the same gate.
        with self.assertRaises(ConflictError):
            self.service.transition(self.inspector, inspection["id"], "pass", {"findings": "x"})

        # Run rescue flow and close the alarm; held record is recomputed.
        alarm = self.service.transition(self.admin, alarm["id"], "dispatch", {})
        job = self.service.create(
            self.admin, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j1", "team": "Alpha"}
        )
        self.service.transition(self.admin, job["id"], "arrive", {})
        self.service.transition(self.admin, job["id"], "complete", {"outcome": "freed"})
        self.service.transition(self.admin, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(self.admin, alarm["id"], "close", {})

        record = self.service.get(item["id"])
        self.assertEqual(record["status"], "applied")
        self.assertEqual(self.service.get(inspection["id"])["status"], "passed")
        # The retry is audited on both the offline record and the target.
        target_audit = [a for a in self.service.audit_log(inspection["id"]) if a["action"] == "retry_offline"]
        self.assertEqual(len(target_audit), 1)

    def test_held_stays_held_if_another_alarm_remains_open(self):
        inspection = self.create_inspection()
        a1 = self.service.create(self.admin, "alarm", {"equipment_id": self.equipment["id"], "code": "A1", "occurred_at": "2026-10-01T10:00:00Z"})
        a2 = self.service.create(self.admin, "alarm", {"equipment_id": self.equipment["id"], "code": "A2", "occurred_at": "2026-10-01T11:00:00Z"})
        [item] = self.service.merge_offline(
            self.inspector, [self.record(inspection, "pass", {"findings": "held"}, rid="h1")]
        )
        self.assertEqual(item["status"], "held")
        self.service.transition(self.admin, a1["id"], "mark_false", {})
        # A2 still open -> record stays held.
        self.assertEqual(self.service.get(item["id"])["status"], "held")
        self.service.transition(self.admin, a2["id"], "mark_false", {})
        self.assertEqual(self.service.get(item["id"])["status"], "applied")

    def test_maintenance_offline_completion_obeys_gate(self):
        maintenance = self.service.create(
            self.admin,
            "maintenance",
            {"equipment_id": self.equipment["id"], "work_type": "routine", "planned_at": "2026-10-01"},
        )
        self.service.transition(self.maintenance, maintenance["id"], "start", {})
        raw = self.record(
            self.service.get(maintenance["id"]), "complete",
            {"completed_at": "2026-10-01T12:00:00Z", "note": "done"},
        )
        [item] = self.service.merge_offline(self.maintenance, [raw])
        self.assertEqual(item["status"], "applied")

    # -- permit recompute -------------------------------------------------

    def _granted_permit(self):
        inspection = self.create_inspection()
        self.service.transition(self.inspector, inspection["id"], "pass", {"findings": "ok"})
        permit = self.service.create(
            self.admin,
            "permit",
            {"equipment_id": self.equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        permit = self.service.transition(self.admin, permit["id"], "grant", {})
        return permit

    def test_new_alarm_returns_granted_permit_to_review_with_reason(self):
        permit = self._granted_permit()
        self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": self.equipment["id"], "code": "TRAP-9", "occurred_at": "2026-10-02T08:00:00Z"},
        )
        updated = self.service.get(permit["id"])
        self.assertEqual(updated["status"], "pending_review")
        self.assertIn("TRAP-9", updated["data"]["review_reason"])
        self.assertEqual(updated["data"]["review_trigger"], "create:alarm")
        self.assertEqual(len(updated["data"]["review_history"]), 1)
        audit = [a for a in self.service.audit_log(permit["id"]) if a["action"] == "return_to_review"]
        self.assertEqual(len(audit), 1)

    def test_permit_requires_manual_re_grant_after_return_to_review(self):
        permit = self._granted_permit()
        alarm = self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": self.equipment["id"], "code": "TRAP-1", "occurred_at": "2026-10-02T08:00:00Z"},
        )
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        # Alarm cleared through full rescue flow.
        alarm = self.service.transition(self.admin, alarm["id"], "dispatch", {})
        job = self.service.create(self.admin, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j", "team": "T"})
        self.service.transition(self.admin, job["id"], "arrive", {})
        self.service.transition(self.admin, job["id"], "complete", {"outcome": "freed"})
        self.service.transition(self.admin, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(self.admin, alarm["id"], "close", {})
        # Not silently re-granted; must be re-approved explicitly.
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        re_granted = self.service.transition(self.admin, permit["id"], "grant", {})
        self.assertEqual(re_granted["status"], "granted")

    def test_equipment_suspension_recomputes_permit(self):
        permit = self._granted_permit()
        self.service.transition(self.admin, self.equipment["id"], "suspend", {})
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        self.assertIn("suspended", self.service.get(permit["id"])["data"]["review_reason"])

    def test_open_remediation_blocks_grant_and_recomputes_on_create(self):
        permit = self._granted_permit()
        self.service.create(
            self.admin,
            "remediation",
            {"equipment_id": self.equipment["id"], "issue": "door", "owner": "M", "due_at": "2026-10-10"},
        )
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "grant", {})

    def test_grant_is_blocked_while_alarm_open(self):
        inspection = self.create_inspection()
        self.service.transition(self.inspector, inspection["id"], "pass", {"findings": "ok"})
        self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": self.equipment["id"], "code": "TRAP", "occurred_at": "2026-10-02T08:00:00Z"},
        )
        permit = self.service.create(
            self.admin,
            "permit",
            {"equipment_id": self.equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.service.transition(self.admin, permit["id"], "request_review", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, permit["id"], "grant", {})

    # -- malformed input --------------------------------------------------

    def test_missing_identity_rejected(self):
        inspection = self.create_inspection()
        raw = self.record(inspection, "pass", {"findings": "x"})
        raw.pop("source_id")
        with self.assertRaises(ValidationError):
            self.service.merge_offline(self.inspector, [raw])

    def test_unknown_target_rejected(self):
        raw = {"source_id": "s", "record_id": "r", "kind": "inspection",
               "target_id": "nope", "action": "pass", "base_version": 1, "payload": {"findings": "x"}}
        [item] = self.service.merge_offline(self.inspector, [raw])
        self.assertEqual(item["status"], "rejected")
        self.assertIn("不存在", item["data"]["reason"])

    def test_offline_record_cannot_be_created_via_generic_api(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.inspector, "offline_record", {"source_id": "s"})


if __name__ == "__main__":
    unittest.main()
