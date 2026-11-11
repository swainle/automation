import sys
import unittest
from pathlib import Path

from automation.pdd.spider.workflow import FatalWorkflowError, StepConditions, Workflow


class WorkflowTests(unittest.TestCase):
    def test_fatal_interface_failure_stops_next_item_but_runs_cleanup(self):
        calls = []

        class Collector:
            def move(self, item):
                calls.append(("move", item))
                raise FatalWorkflowError("WDA 接口不可用")

            def fail_item(self, item, step, error):
                calls.append(("failed", item))

            def finish_item(self, item):
                calls.append(("cleanup", item))

            def close(self):
                calls.append("close")

        workflow = Workflow(Collector(), ["move", "close"], phases={"close": frozenset({"after"})})
        workflow.set_items([1, 2])
        with self.assertLogs("automation.pdd.spider.workflow", level="ERROR"):
            with self.assertRaisesRegex(FatalWorkflowError, "WDA 接口不可用"):
                workflow.run("[move],close")
        self.assertEqual(calls, [("move", 1), ("failed", 1), ("cleanup", 1), "close"])

    def test_before_then_ordered_loop_and_continue_after_failure(self):
        calls = []

        class Collector:
            def prepare(self):
                calls.append("prepare")
                workflow.set_items([1, 2])

            def first(self, item):
                calls.append(("first", item))
                if item == 1:
                    raise ValueError("failed")

            def second(self, item):
                calls.append(("second", item))

            def finish_item(self, item):
                calls.append(("finish", item))

        workflow = Workflow(Collector(), ["prepare", "first", "second"])
        with self.assertLogs("automation.pdd.spider.workflow", level="ERROR"):
            workflow.run("prepare,[first,second]")
        self.assertEqual(calls, ["prepare", ("first", 1), ("finish", 1),
                                 ("first", 2), ("second", 2), ("finish", 2)])

    def test_unknown_steps_are_rejected_before_any_execution(self):
        class Collector:
            def prepare(self):
                self.called = True
        collector = Collector()
        collector.called = False
        with self.assertRaises(ValueError):
            Workflow(collector, ["prepare"]).run("prepare,[unknown]")
        self.assertFalse(collector.called)

    def test_without_list_repeats_until_interrupt(self):
        calls = []

        class Collector:
            def step(self, item):
                calls.append(item)
                if len(calls) == 3:
                    raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            Workflow(Collector(), ["step"], interval=0).run("[step]")
        self.assertEqual(calls, [None, None, None])

    def test_missing_loop_prerequisite_prevents_initialization(self):
        calls = []

        class Collector:
            def prepare(self):
                calls.append("prepare")

            def crop(self, item):
                calls.append("crop")

        workflow = Workflow(Collector(), ["prepare", "crop"], conditions={
            "crop": StepConditions(requires=frozenset({"image"})),
        })
        with self.assertRaisesRegex(ValueError, "loop.*crop.*image"):
            workflow.run("prepare,[crop]")
        self.assertEqual(calls, [])

    def test_later_step_does_not_satisfy_earlier_prerequisite(self):
        class Collector:
            def screenshot(self, item):
                pass

            def crop(self, item):
                pass

        workflow = Workflow(Collector(), ["screenshot", "crop"], conditions={
            "screenshot": StepConditions(provides=frozenset({"image"})),
            "crop": StepConditions(requires=frozenset({"image"})),
        })
        with self.assertRaisesRegex(ValueError, "crop.*image"):
            workflow.validate("[crop,screenshot]")
        workflow.validate("[screenshot,crop]")

    def test_item_requires_an_initialized_list(self):
        class Collector:
            def prepare(self):
                pass

            def scroll_center(self, item):
                pass

        workflow = Workflow(Collector(), ["prepare", "scroll_center"], conditions={
            "prepare": StepConditions(provides=frozenset({"items"})),
            "scroll_center": StepConditions(requires=frozenset({"item"})),
        })
        with self.assertRaisesRegex(ValueError, "scroll_center.*item"):
            workflow.validate("[scroll_center]")
        workflow.validate("prepare,[scroll_center]")
        with self.assertRaisesRegex(ValueError, "未生成循环列表"):
            workflow.run("prepare,[scroll_center]")

    def test_initial_and_invalidated_conditions(self):
        class Collector:
            def crop(self, item):
                pass

            def clear(self, item):
                pass

        workflow = Workflow(Collector(), ["crop", "clear"], available=["image"],
                            conditions={
            "crop": StepConditions(requires=frozenset({"image"})),
            "clear": StepConditions(invalidates=frozenset({"image"})),
        })
        with self.assertRaisesRegex(ValueError, "后续循环.*crop.*image"):
            workflow.validate("[crop,clear]")
        with self.assertRaisesRegex(ValueError, "crop.*image"):
            workflow.validate("[clear,crop]")

    def test_after_runs_once_on_normal_completion_and_interrupt(self):
        for interrupt in (False, True):
            calls = []

            class Collector:
                def step(self, item):
                    calls.append("loop")
                    if interrupt:
                        raise KeyboardInterrupt

                def save(self):
                    calls.append("save")

            workflow = Workflow(Collector(), ["step", "save"])
            workflow.set_items([1])
            if interrupt:
                with self.assertRaises(KeyboardInterrupt):
                    workflow.run("[step],save")
            else:
                workflow.run("[step],save")
            self.assertEqual(calls, ["loop", "save"])

    def test_after_continues_and_preserves_original_error(self):
        calls = []

        class Collector:
            def prepare(self):
                raise RuntimeError("original")

            def save(self):
                calls.append("save")
                raise ValueError("after failed")

            def close(self):
                calls.append("close")

        workflow = Workflow(Collector(), ["prepare", "save", "close"], phases={
            "save": frozenset({"after"}), "close": frozenset({"after"}),
        })
        with self.assertLogs("automation.pdd.spider.workflow", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "original"):
                workflow.run("prepare,save,close")
        self.assertEqual(calls, ["save", "close"])

    def test_validation_failure_does_not_run_after(self):
        calls = []

        class Collector:
            def save(self):
                calls.append("save")

        with self.assertRaises(ValueError):
            Workflow(Collector(), ["save"]).run("[unknown],save")
        self.assertEqual(calls, [])

    def test_after_failure_is_reported_when_loop_succeeds(self):
        class Collector:
            def save(self):
                raise ValueError("save failed")

        with self.assertLogs("automation.pdd.spider.workflow", level="ERROR"):
            with self.assertRaises(ExceptionGroup):
                Workflow(Collector(), ["save"], phases={"save": frozenset({"after"})}).run("save")


if __name__ == "__main__":
    unittest.main()
