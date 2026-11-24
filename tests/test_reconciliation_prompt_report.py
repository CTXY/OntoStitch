"""Prompt previews remain readable and retain exact, escaped wire text."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from build_reconciliation_simplified_report import markdown_prompt, prompt_box


def test_readable_preview_renders_headings_and_data_without_executing_html():
    source='# 本轮任务\n\n请检查 `edge.patient_has_sex`。\n\n## 原始数据\n\n```json\n{"description":"<script>bad()</script>"}\n```'
    html=prompt_box('User 1：固定任务',source)
    assert '<h1>本轮任务</h1>' in html
    assert '<code>edge.patient_has_sex</code>' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '逐字原始文本（实际发给模型）' in html


def test_literal_text_outside_code_is_escaped_and_preserved():
    source='## 当前工作状态\n\n<div onclick="bad()">原始材料</div>'
    html=markdown_prompt(source)
    assert '<div onclick=' not in html and '&lt;div onclick=' in html
    assert '原始材料' in html
