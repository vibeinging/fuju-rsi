"""问数案例的来源检查和结果判分。

这层只处理可复核的案例与结构化结果。真实会话、数据库和最终答复由业务
runner 提供；本模块不会根据被测 Agent 的输出生成标准答案。
"""
from __future__ import annotations

from collections import Counter, deque
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import math
from typing import Callable, Optional

from .benchmark import BenchmarkError, inspect_book
from .core import Evaluation, Prediction


class AskDataError(ValueError):
    pass


def _text(value, name):
    if type(value) is not str or not value.strip():
        raise AskDataError(name + " 必须是非空文本")
    return value


def _identity(value, name):
    _text(value, name)
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise AskDataError(name + " 不能有首尾空格或控制字符")
    return value


def _object(value, name):
    if type(value) is not dict:
        raise AskDataError(name + " 必须是对象")
    return value


def _list(value, name):
    if type(value) is not list:
        raise AskDataError(name + " 必须是列表")
    return value


def _finite_json(value, name):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise AskDataError(name + " 必须是有限 JSON 值") from exc
    return value


def _number(value, name):
    if type(value) not in (int, float):
        raise AskDataError(name + " 必须是数字")
    if type(value) is float and not math.isfinite(value):
        raise AskDataError(name + " 必须是有限数字")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AskDataError(name + " 必须是有限数字") from exc
    if not result.is_finite():
        raise AskDataError(name + " 必须是有限数字")
    return result


def _expected_turn(value):
    value = _object(value, "expected")
    behavior = value.get("behavior")
    if behavior not in ("answer", "clarify", "explain_missing_data"):
        raise AskDataError("expected.behavior 无效")
    if behavior == "answer":
        result = _object(value.get("result"), "expected.result")
        columns = _list(result.get("columns"), "expected.result.columns")
        if any(type(column) is not str or not column.strip() for column in columns):
            raise AskDataError("expected.result.columns 包含无效列名")
        rows = _list(result.get("rows"), "expected.result.rows")
        if any(type(row) is not list or len(row) != len(columns) for row in rows):
            raise AskDataError("expected.result.rows 的列数无效")
        _finite_json(rows, "expected.result.rows")
        if type(result.get("ordered")) is not bool:
            raise AskDataError("expected.result.ordered 必须明确")
        tolerance = _number(result.get("absoluteTolerance", 0), "absoluteTolerance")
        if tolerance < 0:
            raise AskDataError("absoluteTolerance 不能为负")
        if "unit" in result:
            _text(result["unit"], "expected.result.unit")
        if type(value.get("requiresDelivery", True)) is not bool:
            raise AskDataError("requiresDelivery 必须是布尔值")
    else:
        _text(value.get("rubric"), "expected.rubric")
        refs = _list(value.get("basisRefs"), "expected.basisRefs")
        if not refs or any(type(ref) is not str or not ref.strip() for ref in refs):
            raise AskDataError("行为题需要业务依据")
    return value


def _expected(value):
    value = _object(value, "expected")
    if "turns" in value:
        turns = _list(value["turns"], "expected.turns")
        if not turns:
            raise AskDataError("多轮题不能没有轮次")
        for turn in turns:
            _expected_turn(turn)
    else:
        _expected_turn(value)
    return value


def build_casebook(catalog):
    """把一份单一用途的问数目录转为 RSI v2 案例草案。

    来源先确定 split 和 family；案例只能引用来源，不能自行换组或换用途。
    开发与验收目录必须分开保存，本函数不提供跨角色读取权限。
    """
    catalog = _object(catalog, "catalog")
    if catalog.get("schemaVersion") != 1 or catalog.get("role") not in ("development", "holdout"):
        raise AskDataError("问数目录需要 schemaVersion=1 和明确 role")
    role = catalog["role"]
    environment = _object(catalog.get("environment"), "environment")
    if not environment:
        raise AskDataError("environment 不能为空")
    _text(environment.get("dataSnapshot"), "environment.dataSnapshot")
    _text(environment.get("contextRevision"), "environment.contextRevision")
    _finite_json(environment, "environment")
    sources = {}
    refs = set()
    families = {}
    for source in _list(catalog.get("sources"), "sources"):
        source = _object(source, "source")
        source_id = _identity(source.get("id"), "source.id")
        family = _identity(source.get("familyId"), "source.familyId")
        kind = _identity(source.get("kind"), "source.kind")
        ref = _identity(source.get("ref"), "source.ref")
        split = source.get("split")
        if split not in (("train", "validation") if role == "development" else ("test",)):
            raise AskDataError("来源用途与目录 role 不匹配")
        if source_id in sources or ref in refs:
            raise AskDataError("来源身份重复")
        if family in families and families[family] != split:
            raise AskDataError("同一语义家族不能跨用途")
        families[family] = split
        refs.add(ref)
        sources[source_id] = source
    if not sources:
        raise AskDataError("至少需要一个来源")
    cases = []
    by_id = {}
    for item in _list(catalog.get("cases"), "cases"):
        item = _object(item, "case")
        case_id = _text(item.get("id"), "case.id")
        if case_id in by_id:
            raise AskDataError("案例 id 重复")
        source_id = _identity(item.get("sourceId"), "case.sourceId")
        if source_id not in sources:
            raise AskDataError("案例必须引用已登记来源")
        source = sources[source_id]
        if ("split" in item and item["split"] != source["split"]) or (
            "group" in item and item["group"] != source["familyId"]
        ):
            raise AskDataError("案例不能覆盖来源的用途或家族")
        status = item.get("status", "draft")
        if status not in ("draft", "ready", "stale"):
            raise AskDataError("案例状态无效；excluded 案例请留在目录外单独记录")
        single = "question" in item
        if single == ("turns" in item):
            raise AskDataError("案例必须提供 question 或 turns 之一")
        if single:
            input_value = _text(item["question"], "case.question")
            expected = item.get("expected")
        else:
            turns = _list(item["turns"], "case.turns")
            if not turns:
                raise AskDataError("多轮题不能没有轮次")
            input_value = {"turns": [_text(_object(turn, "turn").get("question"), "turn.question")
                                     for turn in turns]}
            expected = {"turns": [turn.get("expected") for turn in turns]}
        tags = _list(item.get("tags"), "case.tags")
        if not tags or any(type(tag) is not str or not tag.strip() for tag in tags):
            raise AskDataError("案例需要覆盖标签")
        parent_ids = _list(item.get("parentCaseIds", []), "case.parentCaseIds")
        if any(type(parent) is not str or not parent.strip() for parent in parent_ids):
            raise AskDataError("父案例 id 无效")
        origin = item.get("origin", "original")
        if origin not in ("original", "variant", "synthetic"):
            raise AskDataError("案例 origin 无效")
        if origin == "variant" and not parent_ids:
            raise AskDataError("改写题必须标明父案例")
        oracle = _object(item.get("oracle", {}), "case.oracle")
        output = {"id": case_id, "input": input_value, "group": source["familyId"],
                  "split": source["split"], "tags": tags,
                  "source": {"kind": source["kind"], "ref": source["ref"]},
                  "origin": origin, "parentCaseIds": parent_ids,
                  "expectedStatus": "draft"}
        if status == "ready":
            if oracle.get("verified") is not True:
                raise AskDataError("ready 案例的标准答案必须已复核")
            _text(oracle.get("evidence"), "oracle.evidence")
            _text(oracle.get("method"), "oracle.method")
            if oracle.get("dataSnapshot") != environment["dataSnapshot"] or (
                oracle.get("contextRevision") != environment["contextRevision"]
            ):
                raise AskDataError("标准答案依赖的数据或业务口径已变化")
            _expected(expected)
            output.update(expected=expected, expectedStatus="verified",
                          evidence=oracle["evidence"])
        elif oracle.get("verified") is True:
            raise AskDataError("已复核答案不能标为 draft 或 stale")
        cases.append(output)
        by_id[case_id] = (source_id, origin, parent_ids)
    if not cases:
        raise AskDataError("至少需要一个案例")
    for case_id, (source_id, origin, parents) in by_id.items():
        for parent in parents:
            if parent not in by_id or by_id[parent][0] != source_id or parent == case_id:
                raise AskDataError("父案例必须属于同一原始来源")
        if origin == "original" and parents:
            raise AskDataError("原题不能有父案例")
    visiting = set()
    visited = set()

    for case_id in by_id:
        stack = [(case_id, False)]
        while stack:
            current, done = stack.pop()
            if done:
                visiting.remove(current)
                visited.add(current)
            elif current in visiting:
                raise AskDataError("案例来源关系不能形成循环")
            elif current not in visited:
                visiting.add(current)
                stack.append((current, True))
                stack.extend((parent, False) for parent in reversed(by_id[current][2]))
    book = {"schemaVersion": 2, "role": role,
            "id": _text(catalog.get("id"), "catalog.id"),
            "name": _text(catalog.get("name"), "catalog.name"),
            "scoring": "ask-data-structured-v1", "environment": environment,
            "cases": cases}
    try:
        inspect_book(book)
    except BenchmarkError as exc:
        raise AskDataError(str(exc)) from exc
    return book


def _canonical(value):
    # 数值转成精确分数，不受 Decimal 的当前精度影响。每种 JSON 类型均带标签，
    # 防止业务对象与内部数值表示碰撞；0 与 -0 也应使用同一个比较键。
    if type(value) in (int, float):
        number = Fraction(_number(value, "cell"))
        return ("number", number.numerator, number.denominator)
    if type(value) is list:
        return ("list", [_canonical(item) for item in value])
    if type(value) is dict:
        return ("object", [(key, _canonical(value[key])) for key in sorted(value)])
    if value is None:
        return ("null",)
    return (type(value).__name__, value)


def _cell_equal(left, right, tolerance):
    if type(left) in (int, float) and type(right) in (int, float):
        return abs(Fraction(_number(left, "expected cell")) -
                   Fraction(_number(right, "actual cell"))) <= Fraction(tolerance)
    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(
            _cell_equal(a, b, tolerance) for a, b in zip(left, right))
    if type(left) is dict:
        return left.keys() == right.keys() and all(
            _cell_equal(left[key], right[key], tolerance) for key in left)
    return left == right


def _row_equal(left, right, tolerance):
    return len(left) == len(right) and all(_cell_equal(a, b, tolerance) for a, b in zip(left, right))


def _unordered_equal(expected, actual, tolerance):
    def key(row):
        return json.dumps(_canonical(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if Counter(map(key, expected)) == Counter(map(key, actual)):
        return True
    if tolerance == 0:
        return False
    # 有容差时找一一匹配。迭代搜索避免多行相同数据触发 Python 递归上限。
    matched_left = [-1] * len(expected)
    matched_right = [-1] * len(actual)
    for start in range(len(expected)):
        queue = deque([start])
        seen_left = {start}
        seen_right = set()
        previous = {}
        found = -1
        while queue and found < 0:
            left = queue.popleft()
            for right, row in enumerate(actual):
                if right in seen_right or not _row_equal(expected[left], row, tolerance):
                    continue
                seen_right.add(right)
                previous[right] = left
                if matched_right[right] < 0:
                    found = right
                    break
                next_left = matched_right[right]
                if next_left not in seen_left:
                    seen_left.add(next_left)
                    queue.append(next_left)
        if found < 0:
            return False
        while found >= 0:
            left = previous[found]
            old = matched_left[left]
            matched_left[left] = found
            matched_right[found] = left
            found = old
    return True


def _score_turn(expected, actual, *, behavior_judge, delivery_judge):
    actual = _object(actual, "actual turn")
    if actual.get("status") != "succeeded":
        raise AskDataError("运行未成功；不能作为业务正确率证据")
    if actual.get("behavior") not in ("answer", "clarify", "explain_missing_data"):
        raise AskDataError("运行结果缺少可信的 behavior 归类")
    if actual["behavior"] != expected["behavior"]:
        return False, "行为类型不符"
    if expected["behavior"] != "answer":
        if behavior_judge is None:
            raise AskDataError("行为题需要独立的 behavior_judge")
        verdict = behavior_judge(expected, actual)
        if type(verdict) is not bool:
            raise AskDataError("behavior_judge 必须返回布尔值")
        return verdict, "行为依据检查失败" if not verdict else ""
    reference = expected["result"]
    result = _object(actual.get("result"), "actual.result")
    if type(result.get("complete")) is not bool:
        raise AskDataError("actual.result.complete 必须明确；预览不能默认全量")
    if not result["complete"]:
        return False, "结果只有部分数据"
    columns = _list(result.get("columns"), "actual.result.columns")
    rows = _list(result.get("rows"), "actual.result.rows")
    if any(type(column) is not str for column in columns) or any(
        type(row) is not list or len(row) != len(columns) for row in rows
    ):
        raise AskDataError("实际结果列或行格式无效")
    _finite_json(rows, "actual.result.rows")
    if columns != reference["columns"] or len(rows) != len(reference["rows"]):
        return False, "列或行数不符"
    if "unit" in reference and result.get("unit") != reference["unit"]:
        return False, "单位不符"
    tolerance = _number(reference.get("absoluteTolerance", 0), "absoluteTolerance")
    if reference["ordered"]:
        same = all(_row_equal(left, right, tolerance)
                   for left, right in zip(reference["rows"], rows))
    else:
        same = _unordered_equal(reference["rows"], rows, tolerance)
    if not same:
        return False, "结果值不符"
    if expected.get("requiresDelivery", True):
        if delivery_judge is None:
            raise AskDataError("最终答复需要独立的 delivery_judge")
        verdict = delivery_judge(expected, actual)
        if type(verdict) is not bool:
            raise AskDataError("delivery_judge 必须返回布尔值")
        if not verdict:
            return False, "最终答复不完整"
    return True, ""


def evaluate(expected, prediction: Prediction, *,
             behavior_judge: Optional[Callable] = None,
             delivery_judge: Optional[Callable] = None) -> Evaluation:
    """完整案例按全轮通过计分；运行或评分协议错误会抛出，不伪装成业务失败。"""
    _expected(expected)
    if not isinstance(prediction, Prediction):
        raise AskDataError("runner 必须返回 Prediction")
    output = _object(prediction.output, "prediction.output")
    if "turns" in expected:
        actual_turns = _list(output.get("turns"), "prediction.output.turns")
        if len(actual_turns) != len(expected["turns"]):
            raise AskDataError("实际轮次数不匹配")
        pairs = zip(expected["turns"], actual_turns)
    else:
        pairs = [(expected, output)]
    failure = None
    for index, (reference, actual) in enumerate(pairs, 1):
        passed, reason = _score_turn(reference, actual, behavior_judge=behavior_judge,
                                     delivery_judge=delivery_judge)
        if not passed and failure is None:
            failure = "第 %d 轮：%s" % (index, reason)
    # 后续轮次的运行/协议异常必须继续抛出，不能被前面的业务零分遮住。
    if failure is not None:
        return Evaluation(0.0, failure)
    return Evaluation(1.0, "全部必要轮次通过")
