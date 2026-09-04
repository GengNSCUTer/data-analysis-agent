from __future__ import annotations

from scripts.post_training.data.generate_olist_pilot_v1_questions import render_question


def test_question_template_supports_every_frozen_time_series_grain() -> None:
    for grain in ("day", "week", "month", "quarter", "year"):
        question = render_question(
            {
                "metric_ids": ["gmv"],
                "result_shape": "time_series",
                "time": {
                    "mode": "series",
                    "start": "2017-01-01",
                    "end_exclusive": "2017-04-01",
                    "grain": grain,
                },
            }
        )
        assert question.startswith("请")
        assert "GMV" in question
