import unittest

from benchmarks.qwen37_flash_vs_plus import build_cases


def response(content="", tool_calls=None):
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": tool_calls or [],
                }
            }
        ]
    }


class Qwen37PairwiseHarnessTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case.case_id: case for case in build_cases()}

    def test_suite_has_ten_unique_direct_api_cases(self):
        self.assertEqual(10, len(self.cases))
        self.assertEqual(3, sum(1 for case in self.cases.values() if case.thinking))

    def test_scheduling_accepts_plain_json_and_penalizes_markdown_wrapping(self):
        case = self.cases["scheduling_logic"]

        self.assertEqual((2, "correct"), case.validator(response('["甲", "丙", "乙", "丁"]')))
        self.assertEqual(
            (1, "correct_but_wrapped"),
            case.validator(response('```json\n["甲", "丙", "乙", "丁"]\n```')),
        )

    def test_code_validator_recognizes_a_correct_chinese_negative_index_explanation(self):
        case = self.cases["code_debug"]
        value = (
            '{"replacement":"for i in range(1, len(items)):",'
            '"cause":"i=0 时 -1 是负索引，会造成首尾误比较。"}'
        )

        self.assertEqual((2, "patch_and_cause_correct"), case.validator(response(value)))

    def test_practical_intent_requires_the_car_to_reach_the_car_wash(self):
        case = self.cases["practical_intent"]

        self.assertEqual((2, "correct"), case.validator(response("开车")))
        self.assertEqual(0, case.validator(response("步行"))[0])

    def test_function_call_accepts_native_object_arguments_from_ollama(self):
        case = self.cases["function_call"]
        tool_calls = [
            {
                "function": {
                    "name": "lookup_inventory",
                    "arguments": {
                        "sku": "A-17",
                        "warehouse": "HZ-2",
                        "include_reserved": False,
                    },
                }
            }
        ]

        self.assertEqual(
            (2, "exact_tool_call"),
            case.validator(response(tool_calls=tool_calls)),
        )

    def test_summary_accepts_equivalent_explicit_uncertainty_phrasing(self):
        case = self.cases["summary_fidelity"]
        content = (
            "杭州17人反馈升级后偶发白屏。工程师称或与缓存有关但未确认。"
            "客服已提退款方案，未明是否全退。暂无修复日期。"
        )

        self.assertEqual((2, "faithful_and_bounded"), case.validator(response(content)))


if __name__ == "__main__":
    unittest.main()
