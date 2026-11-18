import os
import shutil
import tempfile
import unittest

from app import engine, service
from app.stats import compare_continuous, compare_proportions, student_t_ppf, normal_ppf
from app.store import Store


class StatsTest(unittest.TestCase):
    def test_normal_quantile(self):
        self.assertAlmostEqual(normal_ppf(0.975), 1.959963984540054, places=6)

    def test_t_quantile(self):
        self.assertAlmostEqual(student_t_ppf(0.975, 10), 2.22813885, places=5)
        self.assertAlmostEqual(student_t_ppf(0.975, 1000), 1.9623, places=3)

    def test_proportion_test(self):
        result = compare_proportions(100, 1000, 130, 1000)
        self.assertAlmostEqual(result["effect"]["diff"], 0.03)
        self.assertLess(result["effect"]["p_value"], 0.05)
        self.assertGreater(result["effect"]["ci_low"], 0.0)

    def test_continuous_test(self):
        result = compare_continuous([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
        self.assertAlmostEqual(result["effect"]["diff"], 5.0)
        self.assertGreater(result["effect"]["ci_low"], 0.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "test.sqlite3"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, **overrides):
        payload = {
            "id": "exp1",
            "name": "Checkout test",
            "allocation_method": "fixed",
            "metrics": [
                {"key": "exposure", "name": "Exposure", "type": "proportion"},
                {"key": "purchase", "name": "Purchase", "type": "proportion"},
                {"key": "revenue", "name": "Revenue", "type": "continuous", "aggregation": "sum"},
            ],
            "allow_switch": True,
            "cross_device": True,
            "effective_from": "2026-10-01T00:00:00Z",
        }
        payload.update(overrides)
        return service.create_experiment(self.store, payload)


class LifecycleTest(Base):
    MINIMAL = {
        "assignments": [
            {
                "assignment_id": "a1",
                "user_id": "u1",
                "variant": "control",
                "assigned_at": "2026-10-02T00:00:00Z",
                "strata": {},
            },
            {
                "assignment_id": "a2",
                "user_id": "u2",
                "variant": "treatment",
                "assigned_at": "2026-10-02T00:00:00Z",
                "strata": {},
            },
        ],
        "events": [
            {
                "event_id": "e1",
                "assignment_id": "a1",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1,
                "occurred_at": "2026-10-02T01:00:00Z",
            },
            {
                "event_id": "e2",
                "assignment_id": "a2",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1,
                "occurred_at": "2026-10-02T01:00:00Z",
            },
            {
                "event_id": "e3",
                "assignment_id": "a2",
                "event_type": "purchase",
                "metric_key": "purchase",
                "value": 1,
                "occurred_at": "2026-10-02T02:00:00Z",
            },
        ],
    }

    def test_create_and_analyze(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        detail = service.get_experiment_detail(self.store, "exp1")
        itt = detail["latest"]["populations"]["intention_to_treat"]
        self.assertEqual(itt["units"], {"control": 1, "treatment": 1})
        purchase = next(m for m in itt["metrics"] if m["metric"] == "purchase")
        self.assertEqual(purchase["control"]["n"], 1)
        self.assertEqual(purchase["treatment"]["successes"], 1)
        # exposure is not an outcome; everyone has no purchase in control
        self.assertEqual(purchase["control"]["successes"], 0)

    def test_frozen_snapshot_verifies_clean_before_drift(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        detail = service.open_version(
            self.store,
            "exp1",
            {"kind": "pause", "effective_from": "2026-10-05T00:00:00Z", "reason": "bug"},
        )
        paused = detail["versions"][0]
        report = service.verify(self.store, "exp1", paused["id"])
        self.assertEqual(report["verdict"], "clean")
        self.assertTrue(report["snapshot_is_frozen"])
        self.assertEqual(report["new_events"], [])
        self.assertEqual(report["removed_events"], [])
        self.assertEqual(report["new_assignments"], [])
        self.assertEqual(report["late_results"]["events_received_after_close"], 0)
        self.assertEqual(report["late_results"]["examples"], [])

    def test_import_rejects_reused_fact_ids(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        bundle = service.export_experiment(self.store, "exp1")
        bundle["experiment"]["id"] = "exp2"
        with self.assertRaises(ValueError) as ctx:
            service.import_bundle(self.store, bundle)
        self.assertIn("already exist", str(ctx.exception))
        # Nothing was written.
        self.assertIsNone(self.store.get_experiment("exp2"))

    def test_export_import_preserves_frozen_snapshots(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        service.open_version(
            self.store,
            "exp1",
            {"kind": "pause", "effective_from": "2026-10-05T00:00:00Z", "reason": "bug"},
        )
        service.open_version(
            self.store,
            "exp1",
            {
                "kind": "metric_change",
                "effective_from": "2026-10-06T00:00:00Z",
                "reason": "purchase definition changed",
                "config": {"rules": {"changed_metrics": ["purchase"]}},
            },
        )
        bundle = service.export_experiment(self.store, "exp1")
        target = Store(os.path.join(self.dir, "imported.sqlite3"))
        try:
            imported = service.import_bundle(target, bundle)
            self.assertTrue(all(c["status"] == "match" for c in imported["import_check"]))
            source_versions = bundle["versions"]
            imported_versions = target.list_versions("exp1")
            self.assertEqual(len(source_versions), len(imported_versions))
            for source, restored in zip(source_versions, imported_versions):
                self.assertEqual(source["config"], restored.config)
                self.assertEqual(source["status"], restored.status)
                self.assertEqual(
                    source["snapshot"]["event_ids_hash"], restored.snapshot["event_ids_hash"]
                )
                self.assertEqual(
                    source["snapshot"]["included_event_ids"],
                    restored.snapshot["included_event_ids"],
                )
            # v1 was analysed before the metric bump; replaying the lifecycle
            # would wrongly re-freeze it under purchase v2.
            self.assertEqual(imported_versions[0].config["metric_versions"]["purchase"], "1")
            self.assertEqual(imported_versions[-1].config["metric_versions"]["purchase"], "2")
            self.assertEqual(
                service.verify(target, "exp1", imported_versions[0].id)["verdict"], "clean"
            )
        finally:
            target.close()

    def test_pause_freezes_and_late_results_are_detected(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        service.open_version(
            self.store,
            "exp1",
            {"kind": "pause", "effective_from": "2026-10-05T00:00:00Z", "reason": "bug"},
        )
        detail = service.get_experiment_detail(self.store, "exp1")
        paused = detail["versions"][0]
        self.assertEqual(paused["status"], "paused")
        frozen_events = set(paused["snapshot"]["included_event_ids"])
        self.assertEqual(frozen_events, {"e1", "e2", "e3"})

        # A late result arrives after the pause boundary.
        self.store.insert_events(
            "exp1",
            [
                {
                    "event_id": "e4-late",
                    "assignment_id": "a1",
                    "event_type": "purchase",
                    "metric_key": "purchase",
                    "value": 1,
                    "occurred_at": "2026-10-04T02:00:00Z",
                    "received_at": "2026-10-09T00:00:00Z",
                }
            ],
        )
        report = service.verify(self.store, "exp1", paused["id"])
        self.assertEqual(report["verdict"], "drift")
        self.assertIn("e4-late", report["new_events"])
        frozen_after = service.analyze_version(self.store, "exp1", paused["id"])
        self.assertNotIn("e4-late", frozen_after["_included_event_ids"])
        live = service.analyze_version(self.store, "exp1", paused["id"], live=True)
        self.assertIn("e4-late", live["_included_event_ids"])

    def test_rollback_window(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        detail = service.open_version(
            self.store,
            "exp1",
            {
                "kind": "rollback",
                "rollback_to": 1,
                "reason": "guardrail breach",
                "effective_from": "2026-10-06T00:00:00Z",
            },
        )
        versions = detail["versions"]
        self.assertEqual([v["status"] for v in versions], ["rolled_back", "rolled_back"])
        self.assertEqual(versions[1]["window_mode"], "rollback_base")
        # Rolled-back window reproduces the original sample exactly.
        self.assertEqual(
            versions[0]["snapshot"]["event_ids_hash"], versions[1]["snapshot"]["event_ids_hash"]
        )

    def test_metric_change_drops_stale_events(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        service.open_version(
            self.store,
            "exp1",
            {
                "kind": "metric_change",
                "effective_from": "2026-10-03T00:00:00Z",
                "config": {"rules": {"changed_metrics": ["purchase"]}},
                "reason": "purchase now counts refunds",
            },
        )
        # Bump the metric definition; old events (metric_version=1) are stale.
        self.store.insert_events(
            "exp1",
            [
                {
                    "event_id": "e5-new",
                    "assignment_id": "a2",
                    "event_type": "purchase",
                    "metric_key": "purchase",
                    "value": 1,
                    "occurred_at": "2026-10-03T05:00:00Z",
                    "properties": {"metric_version": "2"},
                }
            ],
        )
        result = service.analyze_version(self.store, "exp1", 2)
        self.assertNotIn("e3", result["_included_event_ids"])
        self.assertIn("e5-new", result["_included_event_ids"])
        self.assertGreaterEqual(result["exclusions"]["by_rule"].get("metric_definition_changed", 0), 1)
        purchase = next(
            m for m in result["populations"]["intention_to_treat"]["metrics"] if m["metric"] == "purchase"
        )
        self.assertEqual(purchase["metric_version"], "2")

    def test_version_comparison_warns_on_metric_change(self):
        self.make()
        self.store.insert_assignments("exp1", self.MINIMAL["assignments"])
        self.store.insert_events("exp1", self.MINIMAL["events"])
        service.recompute(self.store, "exp1")
        service.open_version(
            self.store,
            "exp1",
            {
                "kind": "metric_change",
                "effective_from": "2026-10-03T00:00:00Z",
                "config": {"rules": {"changed_metrics": ["purchase"]}},
            },
        )
        cmp = service.compare(self.store, "exp1", 1, 2)
        self.assertTrue(any("changed definition" in w for w in cmp["warnings"]))
        self.assertEqual(len(cmp["metrics"]), 3)


class PopulationTest(Base):
    def _load(self):
        assignments = [
            # u1 stays control, exposed once, buys once -> ITT control
            {
                "assignment_id": "a1",
                "user_id": "u1",
                "variant": "control",
                "assigned_at": "2026-10-02T00:00:00Z",
                "strata": {"country": "US"},
                "device_id": "web",
            },
            # u2 starts control, switches to treatment and buys after the switch
            {
                "assignment_id": "a2",
                "user_id": "u2",
                "variant": "control",
                "assigned_at": "2026-10-02T00:00:00Z",
                "strata": {"country": "US"},
                "device_id": "web",
            },
            {
                "assignment_id": "a2b",
                "user_id": "u2",
                "variant": "treatment",
                "assigned_at": "2026-10-04T00:00:00Z",
                "strata": {"country": "US"},
                "device_id": "ios",
            },
            # u3 treatment, exposed, does not buy
            {
                "assignment_id": "a3",
                "user_id": "u3",
                "variant": "treatment",
                "assigned_at": "2026-10-02T00:00:00Z",
                "strata": {"country": "GB"},
                "device_id": "web",
            },
            # u4 treatment, never exposed -> ITT only
            {
                "assignment_id": "a4",
                "user_id": "u4",
                "variant": "treatment",
                "assigned_at": "2026-10-02T00:00:00Z",
                "strata": {"country": "GB"},
                "device_id": "web",
            },
        ]
        events = [
            {
                "event_id": "e1",
                "assignment_id": "a1",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1,
                "occurred_at": "2026-10-02T01:00:00Z",
            },
            {
                "event_id": "e2",
                "assignment_id": "a1",
                "event_type": "purchase",
                "metric_key": "purchase",
                "value": 1,
                "occurred_at": "2026-10-02T02:00:00Z",
            },
            {
                "event_id": "e3",
                "assignment_id": "a2",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1,
                "occurred_at": "2026-10-02T01:00:00Z",
            },
            {
                "event_id": "e4",
                "assignment_id": "a2b",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1,
                "occurred_at": "2026-10-04T01:00:00Z",
            },
            {
                "event_id": "e5",
                "assignment_id": "a2b",
                "event_type": "purchase",
                "metric_key": "purchase",
                "value": 1,
                "occurred_at": "2026-10-04T02:00:00Z",
            },
            {
                "event_id": "e6",
                "assignment_id": "a3",
                "event_type": "exposure",
                "metric_key": "exposure",
                "value": 1,
                "occurred_at": "2026-10-02T01:00:00Z",
            },
        ]
        self.store.insert_assignments("exp1", assignments)
        self.store.insert_events("exp1", events)
        service.recompute(self.store, "exp1")
        return service.analyze_version(self.store, "exp1", 1)

    def test_itt_versus_as_treated(self):
        self.make()
        result = self._load()
        itt = result["populations"]["intention_to_treat"]
        ast = result["populations"]["as_treated"]
        # ITT: control u1,u2 (u2 counted as control even though it switched)
        self.assertEqual(itt["units"], {"control": 2, "treatment": 2})
        purchase_itt = next(m for m in itt["metrics"] if m["metric"] == "purchase")
        self.assertEqual(purchase_itt["control"]["successes"], 2)  # u1 and u2
        self.assertEqual(purchase_itt["treatment"]["successes"], 0)
        # As-treated: u2's purchase belongs to treatment
        purchase_ast = next(m for m in ast["metrics"] if m["metric"] == "purchase")
        self.assertEqual(purchase_ast["control"]["successes"], 1)  # u1 only
        self.assertEqual(purchase_ast["treatment"]["successes"], 1)  # u2
        # u2 exposed in both arms -> counted once per arm
        self.assertEqual(ast["units"], {"control": 2, "treatment": 2})
        self.assertTrue(any("both arms" in n for n in ast["notes"]))

    def test_per_protocol_removes_crossover(self):
        self.make()
        result = self._load()
        pp = result["populations"]["per_protocol"]
        self.assertEqual(pp["units"], {"control": 1, "treatment": 1})  # u1, u3 (u4 not exposed)
        purchase = next(m for m in pp["metrics"] if m["metric"] == "purchase")
        self.assertEqual(purchase["control"]["successes"], 1)
        self.assertEqual(purchase["treatment"]["successes"], 0)

    def test_repeated_exposure_check(self):
        self.make()
        result = self._load()
        check = next(c for c in result["checks"] if c["key"] == "repeated_exposure")
        self.assertEqual(check["value"], 1)
        self.assertEqual(check["items"][0]["user_id"], "u2")
        self.assertEqual(check["items"][0]["variants"], ["control", "treatment"])

    def test_switching_disallowed_excludes_unit(self):
        self.make(allow_switch=False)
        result = self._load()
        itt = result["populations"]["intention_to_treat"]
        self.assertEqual(itt["units"], {"control": 1, "treatment": 2})
        rules = [i["rule"] for i in result["exclusions"]["unit_level"]]
        self.assertIn("variant_conflict", rules)
        conflict = next(i for i in result["exclusions"]["unit_level"] if i["rule"] == "variant_conflict")
        self.assertEqual(conflict["user_id"], "u2")
        self.assertIn("multiple arms", conflict["reason"])

    def test_attrition_and_funnel(self):
        self.make()
        result = self._load()
        attr = result["attrition"]
        self.assertEqual(attr["by_arm"]["control"]["assigned"], 2)
        self.assertEqual(attr["by_arm"]["treatment"]["no_exposure"], 1)  # u4
        self.assertIsNotNone(attr["differential_exposure_rate"])
        stages = {row["stage"]: row["count"] for row in result["funnel"]}
        self.assertEqual(stages["Distinct units"], 4)
        self.assertEqual(stages["Exposed units (as-treated)"], 3)

    def test_contamination_exclusion_has_reason(self):
        self.make()
        service.open_version(
            self.store,
            "exp1",
            {
                "kind": "config_change",
                "effective_from": "2026-10-02T00:30:00Z",
                "config": {
                    "rules": {
                        "contaminated_external_ids": ["u3"],
                        "contaminated_reasons": {"u3": "bot traffic confirmed by fraud team"},
                    }
                },
            },
        )
        # reload data into the (unchanged) window; analyse the latest version
        self._load()
        latest = self.store.latest_version("exp1")
        result = service.analyze_version(self.store, "exp1", latest.id)
        self.store.replace_exclusions("exp1", latest.id, [
            {"assignment_id": None, "user_id": i["user_id"], "reason": i["reason"],
             "detail": i["detail"], "rule": i["rule"]}
            for i in result["exclusions"]["unit_level"]
        ])
        rules = {i["user_id"]: i for i in result["exclusions"]["unit_level"]}
        self.assertIn("u3", rules)
        self.assertEqual(rules["u3"]["reason"], "bot traffic confirmed by fraud team")
        excluded = self.store.list_exclusions("exp1")
        self.assertTrue(any(e["user_id"] == "u3" for e in excluded))

    def test_stratified_balance(self):
        self.make(allocation_method="stratified", strata_columns=["country"])
        service.simulate(self.store, "exp1", n_units=400, seed=3)
        detail = service.get_experiment_detail(self.store, "exp1")
        balance = detail["latest"]["balance"]
        self.assertEqual(balance["allocation_method"], "stratified")
        self.assertTrue(balance["strata"])
        self.assertFalse(balance["srm"]["alert"])
        self.assertEqual(
            sum(row["total"] for row in balance["strata"]), balance["srm"]["n"]
        )
        self.assertTrue(all(row["expected_treatment_share"] == 0.5 for row in balance["strata"]))
        self.assertFalse(balance["allocation_integrity"]["alert"])

    def test_simulation_end_to_end(self):
        self.make()
        service.simulate(self.store, "exp1", n_units=300, seed=11)
        detail = service.get_experiment_detail(self.store, "exp1")
        result = detail["latest"]
        self.assertGreater(result["diagnostics"]["units"], 0)
        self.assertGreater(result["diagnostics"]["duplicate_events"], 0)
        self.assertEqual(len(result["populations"]), 4)
        for pop in result["populations"].values():
            for metric in pop["metrics"]:
                self.assertIn("effect", metric)


if __name__ == "__main__":
    unittest.main()
