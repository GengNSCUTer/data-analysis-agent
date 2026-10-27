"""Controlled model prose for browser tests, not production answer rendering."""

import json


def model_answer(evidence):
    paragraphs = []
    for index, result in enumerate(evidence["analyses"]):
        op = result["operation"]
        if result["status"] != "ready":
            text = "模型说明：目前证据还不足，" + result["reason"]
        elif op == "trend":
            judgments = [g["judgment"] for g in result["groups"]]
            text = "模型说明：增长判断" + (
                "不成立，反例可见本次计算。"
                if "false" in judgments
                else "需要结合完整期间解读。"
            )
        elif op == "sum":
            text = f"模型说明：选定结果合计为{result['value']}。"
        elif op == "compare":
            text = f"模型说明：两项差额为{result['difference']}。"
        elif op == "distribution":
            text = f"模型说明：中位数为{result['median']}。"
        elif op == "share":
            text = f"模型说明：分子为{result['numerator']}，分母为{result['denominator']}。"
        elif op == "threshold":
            text = f"模型说明：满足阈值的分组有{result['match_count']}组。"
        else:
            text = "模型说明：请结合本次已计算的结果阅读。"
        paragraphs.append(
            {"text": text, "kind": "fact", "evidence_refs": [f"/analyses/{index}"]}
        )
    if not paragraphs:
        for index, row in enumerate(evidence["sample_rows"]):
            text = (
                "模型说明："
                + "，".join(f"{key}为{value}" for key, value in row.items())
                + "。"
            )
            paragraphs.append(
                {
                    "text": text,
                    "kind": "fact",
                    "evidence_refs": [f"/sample_rows/{index}"],
                }
            )
    return json.dumps(
        {
            "paragraphs": paragraphs
            or [
                {
                    "text": "本次查询没有返回数据。",
                    "kind": "fact",
                    "evidence_refs": ["/source_row_count"],
                }
            ]
        },
        ensure_ascii=False,
    )
