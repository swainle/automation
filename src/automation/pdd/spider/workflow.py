"""Parse and execute ordered steps and scoped, nested list loops."""
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from automation.pdd.spider.logging_setup import operation


class FatalWorkflowError(RuntimeError):
    """A service or interface failure that cannot be fixed by the next item."""


@dataclass(frozen=True)
class StepConditions:
    requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset()
    invalidates: frozenset[str] = frozenset()
    loop_requires: frozenset[str] = frozenset()
    item_conditions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Loop:
    body: tuple


def parse_flow(expression: str) -> tuple:
    """Grammar: flow := node (',' node)*; node := name | '[' flow ']'."""
    if len(expression) > 65536:
        raise ValueError("流程字符串过长")
    position = 0
    count = 0

    def whitespace():
        nonlocal position
        while position < len(expression) and expression[position].isspace():
            position += 1

    def sequence(depth):
        nonlocal position, count
        if depth > 32:
            raise ValueError("循环嵌套不能超过 32 层")
        result = []
        while True:
            whitespace()
            if position >= len(expression) or expression[position] == "]":
                raise ValueError(f"流程第 {position + 1} 个字符处缺少步骤，不能使用空循环或尾逗号")
            count += 1
            if count > 4096:
                raise ValueError("流程节点过多")
            if expression[position] == "[":
                position += 1
                body = sequence(depth + 1)
                if position >= len(expression) or expression[position] != "]":
                    raise ValueError("循环缺少右中括号 ]")
                position += 1
                result.append(Loop(body))
            else:
                match = re.match(r"[A-Za-z_][A-Za-z_0-9]*", expression[position:])
                if match is None:
                    raise ValueError(f"流程第 {position + 1} 个字符无效，只允许步骤名、英文逗号和中括号")
                result.append(match.group())
                position += len(match.group())
            whitespace()
            if position >= len(expression) or expression[position] == "]":
                return tuple(result)
            if expression[position] != ",":
                raise ValueError(f"流程第 {position + 1} 个字符处需要英文逗号")
            position += 1

    whitespace()
    if position == len(expression):
        return ()
    nodes = sequence(0)
    if position != len(expression):
        raise ValueError(f"流程第 {position + 1} 个字符处存在多余的右中括号 ]")
    return nodes


def step_names(nodes):
    for node in nodes:
        if isinstance(node, Loop):
            yield from step_names(node.body)
        else:
            yield node


@dataclass
class _Frame:
    state: set[str]
    in_loop: bool = False
    item: object = None
    iteration_items: tuple | None = None
    pending_items: list | None = None
    pending_schema: frozenset[str] | None = None
    generation: int = 0
    current_step: str = ""


class Workflow:
    def __init__(self, collector, allowed_steps: Iterable[str], *,
                 conditions: dict[str, StepConditions] | None = None,
                 phases: dict[str, frozenset[str]] | None = None,
                 root_only: Iterable[str] = (), available: Iterable[str] = (),
                 interval: float = 1.0):
        self.collector = collector
        self.allowed_steps = frozenset(allowed_steps)
        self.conditions = dict(conditions or {})
        self.phases = dict(phases or {})
        self.root_only = frozenset(root_only)
        self.available = frozenset(available)
        if interval < 0:
            raise ValueError("循环间隔不能为负数")
        self.interval = interval
        self._frame = None
        self._initial_items = None
        self._initial_schema = None
        self._item_labels = {"item"} | set().union(
            *(contract.item_conditions for contract in self.conditions.values()))
        unknown = (self.conditions.keys() | self.phases.keys() | self.root_only) - self.allowed_steps
        if unknown:
            raise ValueError(f"未注册的步骤条件: {', '.join(sorted(unknown))}")

    @property
    def items(self):
        if self._frame is None:
            return self._initial_items
        if self._frame.pending_items is not None:
            return self._frame.pending_items
        return self._frame.iteration_items

    def set_items(self, items: Iterable, *, item_conditions: Iterable[str] | None = None):
        values = list(items)
        if self._frame is None:
            self._initial_items = values
            self._initial_schema = frozenset(item_conditions or ())
            return
        frame = self._frame
        contract = self.conditions.get(frame.current_step, StepConditions())
        schema = frozenset(item_conditions) if item_conditions is not None else contract.item_conditions
        frame.pending_items = values
        frame.pending_schema = schema
        frame.generation += 1
        frame.state.add("items")
        if not frame.in_loop:
            self._initial_items, self._initial_schema = values, schema

    def _contract(self, name):
        return self.conditions.get(name, StepConditions())

    def _split(self, nodes):
        loops = [index for index, node in enumerate(nodes) if isinstance(node, Loop)]
        if loops:
            split = loops[-1] + 1
        else:
            split = len(nodes)
            while split and self.phases.get(nodes[split - 1]) == frozenset({"after"}):
                split -= 1
        return nodes[:split], nodes[split:]

    def _phase(self, name, in_loop, after):
        allowed = self.phases.get(name, frozenset({"before", "loop", "after"}))
        phase = "after" if after and (not in_loop or "after" in allowed) else "loop" if in_loop else "before"
        if phase not in allowed:
            raise ValueError(f"步骤 {name} 不能用于 {phase} 阶段")
        if in_loop and name in self.root_only:
            raise ValueError(f"步骤 {name} 仅限顶层流程")
        return phase

    def _requirements(self, name, phase):
        contract = self._contract(name)
        return contract.requires | (contract.loop_requires if phase == "loop" else frozenset())

    def _check(self, name, state, phase):
        missing = self._requirements(name, phase) - state
        if missing:
            raise ValueError(f"{phase} 步骤 {name} 缺少前置条件: {', '.join(sorted(missing))}")

    def _record(self, name, state):
        contract = self._contract(name)
        state.difference_update(contract.invalidates)
        state.update(contract.provides)

    def validate(self, flow):
        nodes = parse_flow(flow) if isinstance(flow, str) else tuple(flow)
        for name in step_names(nodes):
            if name not in self.allowed_steps:
                raise ValueError(f"不支持的步骤: {name}")
            if not callable(getattr(self.collector, name, None)):
                raise ValueError(f"步骤尚未实现: {name}")
        state = set(self.available)
        if self._initial_items is not None:
            state.add("items")
        self._validate_sequence(nodes, state, self._initial_schema, False)
        return nodes

    def _validate_sequence(self, nodes, state, pending_schema, in_loop):
        body, after = self._split(nodes)
        for node in body:
            if isinstance(node, Loop):
                if in_loop and pending_schema is None:
                    raise ValueError("内层循环缺少初始化列表，请在中括号前执行生成列表的步骤")
                child_state = state - self._item_labels
                if pending_schema is not None:
                    child_state |= {"item", "items"} | set(pending_schema)
                final_state = self._validate_sequence(node.body, child_state, None, True)
                if pending_schema is None:
                    try:
                        self._validate_sequence(node.body, final_state, None, True)
                    except ValueError as error:
                        raise ValueError(f"后续循环前置条件不满足: {error}") from error
            else:
                phase = self._phase(node, in_loop, False)
                self._check(node, state, phase)
                self._record(node, state)
                if "items" in self._contract(node).provides:
                    pending_schema = self._contract(node).item_conditions
        # Child loops may execute zero times and never export per-item conditions.
        for name in after:
            phase = self._phase(name, in_loop, True)
            self._check(name, state, phase)
            self._record(name, state)
            if "items" in self._contract(name).provides:
                pending_schema = self._contract(name).item_conditions
        return state

    def run(self, flow):
        nodes = self.validate(flow)
        state = set(self.available)
        if self._initial_items is not None:
            state.add("items")
        frame = _Frame(state, pending_items=self._initial_items, pending_schema=self._initial_schema)
        self._frame = frame
        try:
            self._execute_sequence(nodes, frame, start=True)
        finally:
            self._frame = None

    def _invoke(self, name, frame, after=False):
        phase = self._phase(name, frame.in_loop, after)
        self._check(name, frame.state, phase)
        frame.current_step = name
        generation = frame.generation
        method = getattr(self.collector, name)
        spec = getattr(method, "step", None)
        description = getattr(spec, "description", name)
        with operation(f"[步骤] {description}"):
            if frame.in_loop:
                method(frame.item)
            else:
                method()
            if "items" in self._contract(name).provides and frame.generation == generation:
                raise ValueError(f"步骤 {name} 未生成循环列表")
        self._record(name, frame.state)

    def _execute_sequence(self, nodes, frame, start=False):
        body, after = self._split(nodes)
        original_error = None
        try:
            if start:
                prepare = getattr(self.collector, "start_runtime", None)
                if prepare:
                    prepare()
            for node in body:
                if isinstance(node, Loop):
                    self._iterate(node.body, frame)
                else:
                    self._invoke(node, frame)
        except BaseException as error:
            original_error = error
            raise
        finally:
            failures = []
            for name in after:
                try:
                    self._invoke(name, frame, after=True)
                except Exception as error:
                    logging.getLogger(__name__).exception("收尾步骤 %s 失败，继续后续收尾步骤", name)
                    failures.append(error)
            if failures and original_error is None:
                raise ExceptionGroup("收尾步骤执行失败", failures)

    def _iterate(self, body, parent):
        if parent.in_loop and parent.pending_items is None:
            raise ValueError("内层循环未生成初始化列表")
        values = tuple(parent.pending_items) if parent.pending_items is not None else None
        schema = parent.pending_schema or frozenset()
        index = 0
        while values is None or index < len(values):
            item = None if values is None else values[index]
            state = parent.state - self._item_labels
            if values is not None:
                state |= {"item", "items"} | set(schema)
            child = _Frame(state, in_loop=True, item=item,
                           iteration_items=values)
            self._frame = child
            try:
                self._execute_sequence(body, child)
            except Exception as error:
                fatal = isinstance(error, FatalWorkflowError)
                logging.getLogger(__name__).exception(
                    "采集基础接口不可用，停止当前流程" if fatal else "采集步骤失败，继续下一个任务")
                report = getattr(self.collector, "fail_item", None)
                if report:
                    try:
                        report(item, child.current_step, error)
                    except Exception:
                        logging.getLogger(__name__).exception("记录采集失败状态时出错")
                if fatal:
                    raise
            finally:
                try:
                    cleanup = getattr(self.collector, "finish_item", None)
                    if cleanup:
                        cleanup(item)
                except Exception:
                    logging.getLogger(__name__).exception("任务清理失败，继续下一个任务")
                finally:
                    self._frame = parent
            index += 1
            if values is None:
                time.sleep(self.interval)
