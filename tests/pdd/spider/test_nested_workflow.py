import sys
import unittest
from pathlib import Path

from automation.pdd.spider.workflow import Loop, StepConditions, Workflow, parse_flow, step_names


def produces_items(*labels):
    return StepConditions(provides=frozenset({"items"}), item_conditions=frozenset(labels))


class ParserTests(unittest.TestCase):
    def test_nested_array_and_whitespace(self):
        nodes = parse_flow(" a, [b, [d,e], f], g ")
        self.assertEqual(nodes, ("a", Loop(("b", Loop(("d", "e")), "f")), "g"))
        self.assertEqual(list(step_names(nodes)), ["a", "b", "d", "e", "f", "g"])
        self.assertEqual(parse_flow("  "), ())

    def test_invalid_and_executable_expressions_are_rejected(self):
        for expression in ("[]", "[a", "a]", "a,,b", "[a,]", "a b", "a，[b]",
                           "__import__('os')", "a;exit", "[[]]"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                parse_flow(expression)

    def test_excessive_nesting_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "32"):
            parse_flow("[" * 33 + "a" + "]" * 33)


class NestedWorkflowTests(unittest.TestCase):
    def test_plain_steps_execute_once(self):
        calls = []

        class Collector:
            def step(self):
                calls.append("once")

        Workflow(Collector(), ["step"]).run("step")
        self.assertEqual(calls, ["once"])

    def test_three_levels_preserve_parent_items_and_order(self):
        calls = []

        class Collector:
            def children(self, item):
                workflow.set_items([item + "1", item + "2"])

            def grandchildren(self, item):
                workflow.set_items([item + "x"])

            def visit(self, item):
                calls.append(item)

            def end_child(self, item):
                calls.append(("child", item))

            def end_parent(self, item):
                calls.append(("parent", item))

            def finish(self):
                calls.append(tuple(workflow.items))

        workflow = Workflow(Collector(), ["children", "grandchildren", "visit", "end_child", "end_parent", "finish"],
                            conditions={"children": produces_items(), "grandchildren": produces_items()})
        workflow.set_items(["A", "B"])
        workflow.run("[children,[grandchildren,[visit],end_child],end_parent],finish")
        self.assertEqual(calls, ["A1x", ("child", "A1"), "A2x", ("child", "A2"),
            ("parent", "A"), "B1x", ("child", "B1"), "B2x", ("child", "B2"),
            ("parent", "B"), ("A", "B")])

    def test_missing_child_list_is_rejected_before_parent_step(self):
        calls = []

        class Collector:
            def step(self, item):
                calls.append(item)

        workflow = Workflow(Collector(), ["step"])
        workflow.set_items([1])
        with self.assertRaisesRegex(ValueError, "内层循环缺少初始化列表"):
            workflow.run("[step,[step]]")
        self.assertEqual(calls, [])

    def test_empty_child_list_skips_only_that_parent_children(self):
        calls = []

        class Collector:
            def children(self, item):
                workflow.set_items([] if item == 1 else ["child"])

            def visit(self, item):
                calls.append(item)

            def save(self, item):
                calls.append(item)

        workflow = Workflow(Collector(), ["children", "visit", "save"],
                            conditions={"children": produces_items()})
        workflow.set_items([1, 2])
        workflow.run("[children,[visit],save]")
        self.assertEqual(calls, [1, "child", 2])

    def test_interrupt_runs_nested_and_root_after_steps(self):
        calls = []

        class Collector:
            def children(self, item):
                workflow.set_items(["child"])

            def visit(self, item):
                raise KeyboardInterrupt

            def end_parent(self, item):
                calls.append(item)

            def finish(self):
                calls.append("finished")

        workflow = Workflow(Collector(), ["children", "visit", "end_parent", "finish"],
                            conditions={"children": produces_items()})
        workflow.set_items(["parent"])
        with self.assertRaises(KeyboardInterrupt):
            workflow.run("[children,[visit],end_parent],finish")
        self.assertEqual(calls, ["parent", "finished"])

    def test_child_outputs_cannot_be_used_after_empty_loop(self):
        class Collector:
            def capture(self, item=None):
                pass

            def crop(self):
                pass

        workflow = Workflow(Collector(), ["capture", "crop"], conditions={
            "capture": StepConditions(provides=frozenset({"image"})),
            "crop": StepConditions(requires=frozenset({"image"})),
        })
        with self.assertRaisesRegex(ValueError, "crop.*image"):
            workflow.validate("[capture],crop")


if __name__ == "__main__":
    unittest.main()
