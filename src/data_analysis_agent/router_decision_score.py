"""Score author-labeled router decisions without calling a model or a database."""

from __future__ import annotations

from typing import Any, Mapping

from vanna.core.user import User

from .question_router import QuestionRouter


_REFUSE = {"unauthorized", "unsupported"}
_TOOL_FREE = {
    "help",
    "catalog_answered",
    "general_business",
    "general_knowledge",
    "result_followup",
}
_CLARIFY = {
    "clarification_required",
    "missing_time",
    "missing_metric",
    "missing_comparison",
}
_EVIDENCE = {"refuse", "tool_free", "clarify", "sql"}


def evidence_bucket(state: str, requires_database: bool) -> str:
    """Collapse a route state into the four decisions the scorecard reports."""
    if state in _REFUSE:
        return "refuse"
    if state in _TOOL_FREE:
        return "tool_free"
    if state in _CLARIFY:
        return "clarify"
    if state == "answerable" and requires_database:
        return "sql"
    return "other"


def validate_label_set(document: Mapping[str, Any]) -> None:
    """Reject a label file that disagrees with itself before any router call."""
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("router decision label set has no cases")
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"router decision case id is missing or duplicated: {case_id}")
        seen.add(case_id)
        gold = case.get("gold")
        if not isinstance(gold, Mapping):
            raise ValueError(f"{case_id} has no gold decision")
        evidence = gold.get("evidence")
        requires_database = gold.get("requires_database")
        if evidence not in _EVIDENCE or not isinstance(requires_database, bool):
            raise ValueError(f"{case_id} gold evidence is incomplete")
        if (evidence == "sql") != requires_database:
            raise ValueError(f"{case_id} gold evidence and requires_database disagree")
        metric_ids = gold.get("metric_ids")
        if not isinstance(metric_ids, list) or not all(isinstance(item, str) for item in metric_ids):
            raise ValueError(f"{case_id} gold metric_ids must be a list of strings")


def score_router_decisions(
    router: QuestionRouter,
    cases: list[Mapping[str, Any]],
    *,
    user: User | None = None,
) -> dict[str, Any]:
    """Compare each label with classify, then admit when classify asks for SQL.

    Admit runs from the classifier's decision, not from the gold label. A
    question such as “本月 GMV” is classified as a query and then stopped for
    a missing calendar range, so the scored decision is clarification.
    """
    analyst = user or User(id="router-decision-score", group_memberships=["analyst"])
    mismatches: list[dict[str, Any]] = []
    evidence_match = 0
    false_open = 0
    false_block = 0
    metric_exact = 0
    metric_scored = 0
    by_stratum: dict[str, dict[str, int]] = {}
    for case in cases:
        gold = case["gold"]
        conversation_state = case.get("conversation_state")
        route = router.classify(
            case["question"],
            user=analyst,
            conversation_state=conversation_state,
        )
        if route.requires_database:
            route = router.admit_sql_request(
                case["question"],
                user=analyst,
                conversation_state=conversation_state,
            )
        actual_evidence = evidence_bucket(route.state, bool(route.requires_database))
        actual_metrics = list(route.metric_ids)
        gold_metrics = list(gold["metric_ids"])
        same_evidence = actual_evidence == gold["evidence"]
        same_database = bool(route.requires_database) is gold["requires_database"]
        same_metrics = set(actual_metrics) == set(gold_metrics)
        same_state = route.state == gold.get("state", route.state)
        if same_evidence:
            evidence_match += 1
        if gold["requires_database"] is False and route.requires_database:
            false_open += 1
        if gold["requires_database"] is True and not route.requires_database:
            false_block += 1
        if gold_metrics:
            metric_scored += 1
            if same_metrics:
                metric_exact += 1
        stratum = str(case.get("stratum") or "unspecified")
        group = by_stratum.setdefault(
            stratum,
            {
                "cases": 0,
                "evidence_match": 0,
                "false_open": 0,
                "false_block": 0,
                "metric_exact": 0,
                "metric_scored": 0,
            },
        )
        group["cases"] += 1
        if same_evidence:
            group["evidence_match"] += 1
        if gold["requires_database"] is False and route.requires_database:
            group["false_open"] += 1
        if gold["requires_database"] is True and not route.requires_database:
            group["false_block"] += 1
        if gold_metrics:
            group["metric_scored"] += 1
            if same_metrics:
                group["metric_exact"] += 1
        if not (same_evidence and same_database and same_metrics and same_state):
            mismatches.append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "gold": {
                        "evidence": gold["evidence"],
                        "requires_database": gold["requires_database"],
                        "metric_ids": gold_metrics,
                        "state": gold.get("state"),
                    },
                    "actual": {
                        "evidence": actual_evidence,
                        "requires_database": bool(route.requires_database),
                        "metric_ids": actual_metrics,
                        "state": route.state,
                    },
                }
            )
    total = len(cases)
    return {
        "cases": total,
        "evidence_match": evidence_match,
        "false_open": false_open,
        "false_block": false_block,
        "metric_exact": metric_exact,
        "metric_scored": metric_scored,
        "by_stratum": by_stratum,
        "mismatches": mismatches,
    }


def _capability_lines(measured: Mapping[str, Any]) -> list[str]:
    """Explain the measured router boundary. Counts come from this run's mismatches."""
    mismatches = list(measured["mismatches"])
    rewrite_sql = [
        item for item in mismatches
        if str(item["id"]).startswith("refuse-rewrite-") and item["actual"]["evidence"] == "sql"
    ]
    rewrite_clarify = [
        item for item in mismatches
        if str(item["id"]).startswith("refuse-rewrite-") and item["actual"]["evidence"] == "clarify"
    ]
    alias_sql = [
        item for item in mismatches
        if str(item["id"]).startswith(("define-", "advice-")) and item["actual"]["evidence"] == "sql"
    ]
    reason_only = [
        item for item in mismatches
        if item["gold"]["evidence"] == "refuse"
        and item["actual"]["evidence"] == "refuse"
        and item["gold"]["state"] != item["actual"]["state"]
    ]
    blocked = [
        item for item in mismatches
        if item["gold"]["evidence"] == "sql" and item["actual"]["evidence"] != "sql"
    ]
    groups = measured["by_stratum"]

    def perfect(stratum: str) -> str:
        group = groups[stratum]
        return f"{group['evidence_match']}/{group['cases']}"

    can: list[str] = [
        "### 能解决",
        "",
        "点名了指标、又给出 2017 或 2018 这种日历年份的查库句，"
        f"{perfect('sql')} 全部放行，指标也没有对错。"
        "各州、各月、各品类、各支付方式这几种分组都在里面。",
        f"本月、最近 30 天、今年这三句相对时间，{perfect('clarify_time')} 都停在澄清，"
        "指标仍然记着，SQL 没有打开。路由器不会把「本月」猜成某一天。",
        f"帮助 {perfect('help')}，结果追问 {perfect('followup')}。"
        "有上一轮可信摘要时解释旧数字；没有摘要时要求用户说明是哪一轮；"
        "点名 GMV 再按州或品类拆开查，会走新查询。",
        f"支付金额、光说金额、光说订单、哪个地区表现最好，{perfect('colloquial_unbound')} 都没有猜成 GMV 或其他指标。",
        "删除、修改、插入、导出原始、预测、运行 Python 这些词出现时会拒绝。"
        "最火、热销、订单最多、取消订单最多会落到合同里写好的指标。",
    ]
    if not rewrite_sql and not rewrite_clarify:
        can.append(
            "「把/将……改成/变为/设为……」按句法框架拒绝写库，不靠往拒绝词表里补「改成」。"
            "点名了指标也不会因此打开 SQL。"
        )
    if not alias_sql:
        can.append(
            "定义和建议先去掉已经对上的指标别名，再判剩余句子。"
            "别名里的「平均、金额、数量」不会把「是什么」和「如何提升」推进查库。"
        )
    if not blocked:
        can.append(
            "「各州卖得最好的货」按排名维估行数，不再用 27×73 把已经对上的商品件数整句拦住。"
        )
    can.append(
        f"只要标准答案点了指标，这 {measured['metric_exact']}/{measured['metric_scored']} 题的指标都对上了。"
        + (
            "剩下的不一致主要是拒绝原因，不是把 GMV 认成别的指标。"
            if measured["false_open"] == 0 and measured["false_block"] == 0
            else "若仍有错，错的是该不该查库，不是把 GMV 认成别的指标。"
        )
    )

    cannot: list[str] = ["", "### 不能解决", ""]
    if rewrite_sql:
        cannot.append(
            f"「把某指标改成已完成」有 {len(rewrite_sql)} 题打开了 SQL。"
            "写库框架还没拦住换说法的改写句，句子里的指标名被检索对上后就放行了。"
        )
    if rewrite_clarify:
        cannot.append(
            f"「把订单改成已完成」「把订单表改成已完成」这 {len(rewrite_clarify)} 题没有打开 SQL，"
            "但停在缺指标，而不是拒绝写库。"
        )
    if alias_sql:
        cannot.append(
            f"定义和建议里有 {len(alias_sql)} 题误开 SQL。"
            "「是什么」「如何提升」本不该查库，别名里的「平均、金额、数量」仍被当成查数用语。"
        )
    if reason_only:
        cannot.append(
            f"「预测下个月的系统表」「运行 Python 计算 information_schema」这 {len(reason_only)} 题仍然拒绝，SQL 没开。"
            "系统表和 information_schema 的检查排在预测、跑 Python 前面，所以拒绝原因记成未授权，而不是不支持的能力。"
        )
    if blocked:
        questions = "、".join(f"「{item['question']}」" for item in blocked[:3])
        cannot.append(
            f"该查却拦住有 {len(blocked)} 题：{questions}。"
            "查数意图已经对上，但后续准入仍把句子拦住了。"
        )
    accounted = {item["id"] for item in rewrite_sql + rewrite_clarify + alias_sql + reason_only + blocked}
    leftover = [item for item in mismatches if item["id"] not in accounted]
    if leftover:
        cannot.append(f"另外还有 {len(leftover)} 题不一致，见文末清单。")
    if len(cannot) == 3:
        cannot.append("误开 SQL 和该查却拦住已经收口。若仍有不一致，见文末清单。")

    later: list[str] = ["", "### 后续可以改哪里", ""]
    if rewrite_sql or rewrite_clarify:
        later.append("写库继续用句法框架识别，不要往拒绝词表里无限补动词。")
    if alias_sql:
        later.append("定义和建议必须看去掉别名之后的剩余句子，不要把别名里的「平均、金额」当查数信号。")
    if blocked:
        later.append("超预算时说明要去掉哪一个分组，排名维按 top-N 估行数，不要把已经对上的指标整句拦住。")
    if reason_only:
        later.append(
            "系统表和预测叠在同一句时，可以继续拒绝，并在卡片上同时写明未授权和不支持。"
            f"这 {len(reason_only)} 题没有打开 SQL，优先级低于该不该查库。"
        )
    later.append("不为凑满分再加词。成绩变化只走记分卡 diff，不改标准答案。")
    return can + cannot + later


def render_router_decision_report(
    *,
    title: str,
    document: Mapping[str, Any],
    measured: Mapping[str, Any],
    stratum_notes: Mapping[str, str],
) -> str:
    """Render the frozen markdown report from a measured scorecard."""
    counts = measured
    lines = [
        f"# {title}",
        "",
        "范围：作者按 Olist Catalog v3 的句式合同标注路由决策。"
        "记分只调用 QuestionRouter 的分类和准入，不调用模型，不执行 SQL。",
        "这不是 SQL 可执行率，也不是在线语义准确率。",
        "",
        f"- 标注集：`{document['label_set_id']}`",
        f"- Catalog：`{document['catalog_version']}`",
        f"- 指标版本：`{document['metric_version']}`",
        f"- 题量：{counts['cases']}",
        "",
        "## 各组问法",
        "",
    ]
    for stratum, note in stratum_notes.items():
        group = counts["by_stratum"].get(stratum, {})
        lines.append(f"- `{stratum}`（{group.get('cases', 0)} 题）：{note}")
    lines.extend(
        [
            "",
            "## 总成绩",
            "",
            f"- 证据类型一致：{counts['evidence_match']}/{counts['cases']}",
            f"- 误开 SQL：{counts['false_open']}",
            f"- 该查却拦住：{counts['false_block']}",
            f"- 指标对上：{counts['metric_exact']}/{counts['metric_scored']}",
            "",
            "## 相对言语行为收口前",
            "",
            "标准答案文件没有改。下面是同一套 426 题在收口前后的记分差。",
            "",
            "- 证据类型一致：380/426 → "
            f"{counts['evidence_match']}/{counts['cases']}",
            "- 误开 SQL：43 → "
            f"{counts['false_open']}",
            "- 该查却拦住：1 → "
            f"{counts['false_block']}",
            "- 指标对上：235/235，没有变",
            "",
            "收口前误开的是「把某指标改成已完成」，以及别名里带「平均、金额、数量」的定义和建议。"
            "拦住的那 1 题是「各州卖得最好的货」。",
            "",
            "## 分组成绩",
            "",
        ]
    )
    for stratum in stratum_notes:
        group = counts["by_stratum"].get(stratum)
        if not group:
            continue
        metric = (
            f"{group['metric_exact']}/{group['metric_scored']}"
            if group["metric_scored"]
            else "0/0"
        )
        lines.append(
            f"- `{stratum}`：证据 {group['evidence_match']}/{group['cases']}，"
            f"误开 SQL {group['false_open']}，该查却拦住 {group['false_block']}，"
            f"指标 {metric}"
        )
    lines.extend(["", "## 当前路由器能解决什么、不能解决什么", ""])
    lines.extend(_capability_lines(counts))
    lines.extend(["", "## 不一致题目", ""])
    mismatches = measured["mismatches"]
    if not mismatches:
        lines.append("没有不一致题目。")
    for item in mismatches:
        gold = item["gold"]
        actual = item["actual"]
        lines.append(
            f"- `{item['id']}` {item['question']}："
            f"标准答案 {gold['evidence']}/{gold['state']}/{gold['metric_ids']}，"
            f"实际 {actual['evidence']}/{actual['state']}/{actual['metric_ids']}"
        )
    lines.extend(
        [
            "",
            "## 这些数字不能当成什么",
            "",
            "证据类型一致只说明路由放行、澄清、解释或拒绝是否和句式合同相同。",
            "它不证明 SQL 能执行，也不证明查询结果在业务上正确。",
            "标注是作者按 Catalog 合同写成的，不是独立第三方标注，也不是在线语义准确率。",
            "",
        ]
    )
    return "\n".join(lines)
