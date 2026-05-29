from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from ai_client import make_openai_compatible_client, resolve_ai_credentials


DEFAULT_MAX_TRANSCRIPT_CHARS = 60000


@dataclass
class SummaryResult:
    status: str
    summary_html: str
    model: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _list_items(values: Any) -> str:
    if not values:
        return "<li>未提及或无法从文本中可靠识别。</li>"
    if isinstance(values, str):
        values = [values]
    return "".join(f"<li>{_escape(value)}</li>" for value in values)


def _asset_sections_html(sections: Any) -> str:
    if not isinstance(sections, list) or not sections:
        return "<p>未能从文本中可靠识别具体股票、指数、行业或宏观主题。</p>"

    blocks: List[str] = []
    for raw_section in sections:
        if not isinstance(raw_section, dict):
            continue
        name = raw_section.get("name") or "未命名主题"
        kind = raw_section.get("type") or "unknown"
        stance = raw_section.get("stance") or "不明确"
        creator_view = raw_section.get("creator_view") or "未明确提及。"
        factual_context = raw_section.get("factual_context") or "未明确提及。"
        key_points = raw_section.get("key_points") or []
        risks = raw_section.get("risks") or []
        blocks.append(
            f"<section>"
            f"<h3>{_escape(name)} <small>({_escape(kind)} / {_escape(stance)})</small></h3>"
            f"<p><strong>创作者观点：</strong>{_escape(creator_view)}</p>"
            f"<p><strong>事实背景：</strong>{_escape(factual_context)}</p>"
            f"<p><strong>要点：</strong></p><ul>{_list_items(key_points)}</ul>"
            f"<p><strong>不确定性或风险：</strong></p><ul>{_list_items(risks)}</ul>"
            f"</section>"
        )
    return "".join(blocks) if blocks else "<p>未能从文本中可靠识别具体主题。</p>"


def _render_structured_digest(
    video: Dict[str, Any],
    content_source: str,
    data: Dict[str, Any],
    transcript_note: Optional[str] = None,
) -> str:
    title = data.get("title") or video.get("title")
    watch_original = data.get("watch_original") or "只看部分"
    disclaimer = data.get("disclaimer") or "该摘要仅用于信息整理，不构成投资建议"

    note_html = ""
    if transcript_note:
        note_html = f"<p><strong>文本质量提示：</strong>{_escape(transcript_note)}</p>"

    return (
        f"<h2>{_escape(title)}</h2>"
        f"<p><strong>来源频道：</strong>{_escape(video.get('channel_name'))}</p>"
        f"<p><strong>原视频链接：</strong><a href=\"{_escape(video.get('url'))}\">{_escape(video.get('url'))}</a></p>"
        f"<p><strong>发布时间：</strong>{_escape(video.get('published') or '未知')}</p>"
        f"<p><strong>内容来源：</strong>{_escape(content_source)}</p>"
        f"{note_html}"
        f"<p><strong>一句话总结：</strong>{_escape(data.get('one_sentence_summary') or '摘要生成结果未包含该字段。')}</p>"
        f"<h3>按股票 / 指数 / 行业 / 宏观主题拆分</h3>{_asset_sections_html(data.get('asset_sections'))}"
        f"<h3>核心要点</h3><ul>{_list_items(data.get('core_key_points'))}</ul>"
        f"<h3>关键市场观点</h3><ul>{_list_items(data.get('market_views'))}</ul>"
        f"<h3>涉及的股票、指数、行业或宏观事件</h3><ul>{_list_items(data.get('mentioned_assets_events'))}</ul>"
        f"<h3>对普通投资者的启发</h3><ul>{_list_items(data.get('investor_takeaways'))}</ul>"
        f"<p><strong>值不值得看原视频：</strong>{_escape(watch_original)}</p>"
        f"<h3>需要注意的不确定性或风险</h3><ul>{_list_items(data.get('uncertainties_or_risks'))}</ul>"
        f"<p><strong>免责声明：</strong>{_escape(disclaimer)}</p>"
    )


def render_link_only_digest(
    video: Dict[str, Any],
    reason: Optional[str] = None,
    content_source: str = "metadata only",
) -> str:
    reason_text = reason or "Transcript and ASR were unavailable, so only the original video link is provided."
    return (
        f"<h2>{_escape(video.get('title'))}</h2>"
        f"<p><strong>来源频道：</strong>{_escape(video.get('channel_name'))}</p>"
        f"<p><strong>原视频链接：</strong><a href=\"{_escape(video.get('url'))}\">{_escape(video.get('url'))}</a></p>"
        f"<p><strong>发布时间：</strong>{_escape(video.get('published') or '未知')}</p>"
        f"<p><strong>内容来源：</strong>{_escape(content_source)}</p>"
        f"<p>{_escape(reason_text)}</p>"
        "<p><strong>免责声明：</strong>该摘要仅用于信息整理，不构成投资建议</p>"
    )


def _extract_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _response_content(response: Any) -> str:
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        if dumped.get("error"):
            raise RuntimeError(json.dumps(dumped["error"], ensure_ascii=False))
        choices = dumped.get("choices")
        if not choices:
            raise RuntimeError("LLM response did not include choices")
        message = choices[0].get("message") or {}
        return message.get("content") or "{}"

    choices = getattr(response, "choices", None)
    if not choices:
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(str(error))
        raise RuntimeError("LLM response did not include choices")
    return choices[0].message.content or "{}"


def _call_openai_json(client: Any, model: str, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = _response_content(response)

    return _extract_json(content)


def summarize_transcript(
    video: Dict[str, Any],
    transcript_text: str,
    content_source: str,
    summary_config: Dict[str, Any],
) -> SummaryResult:
    if not transcript_text.strip():
        return SummaryResult(status="link_only", summary_html=render_link_only_digest(video))

    model = os.getenv("SUMMARY_MODEL") or os.getenv("OPENAI_SUMMARY_MODEL") or summary_config.get("model", "gpt-4o-mini")
    try:
        credentials = resolve_ai_credentials(summary_config, default_provider="hkust_gz")
        client = make_openai_compatible_client(credentials)
    except Exception as exc:
        html_text = render_link_only_digest(
            video,
            f"LLM summary was unavailable because AI credentials are not configured: {exc}. The original video link is provided.",
            content_source=content_source,
        )
        return SummaryResult(status="failed", model=model, summary_html=html_text, error_message=str(exc))

    max_chars = int(summary_config.get("max_transcript_chars", DEFAULT_MAX_TRANSCRIPT_CHARS))
    truncated = len(transcript_text) > max_chars
    text_for_model = transcript_text[:max_chars]
    transcript_note = "字幕或转写文本被截断用于摘要，摘要可能不覆盖视频后半部分。" if truncated else None

    max_key_points = int(summary_config.get("max_key_points", 8))
    system_prompt = (
        "You summarize finance YouTube transcripts for an RSS digest. "
        "Write in Simplified Chinese. Do not invent claims. Separate creator opinions from factual market information. "
        "Preserve ticker symbols and company names. Do not provide personal investment advice. "
        "Return strict JSON only."
    )
    user_prompt = f"""
请根据下面的 YouTube 视频文本生成中文摘要。只使用文本中有依据的信息；如果文本质量差、不完整或像机器字幕，请明确说明。

视频元数据：
- 标题：{video.get('title')}
- 来源频道：{video.get('channel_name')}
- 原视频链接：{video.get('url')}
- 发布时间：{video.get('published') or '未知'}
- 内容来源：{content_source}

请返回 JSON object，字段必须是：
{{
  "title": "标题",
  "one_sentence_summary": "一句话总结",
  "asset_sections": [
    {{
      "name": "股票代码/公司名/指数/行业/宏观主题，例如 NVDA / 英伟达、SPY / 标普500、美元指数、AI软件",
      "type": "stock / index / sector / macro / other",
      "stance": "偏多 / 偏空 / 中性 / 不明确",
      "creator_view": "创作者对这个标的或主题的观点；如果只是事实信息，请写未明确表达观点",
      "factual_context": "文本中提到的事实背景，例如财报、PCE、利率、油价、公司业务变化",
      "key_points": ["围绕该标的或主题的 2-5 个要点，适合 RSS 子弹列表展示"],
      "risks": ["围绕该标的或主题的不确定性、风险或文本证据不足之处"]
    }}
  ],
  "core_key_points": ["5-{max_key_points} 个核心要点"],
  "market_views": ["关键市场观点，注明哪些是创作者观点"],
  "mentioned_assets_events": ["涉及的股票、指数、行业或宏观事件"],
  "investor_takeaways": ["对普通投资者的启发，不能构成个性化投资建议"],
  "watch_original": "值得 / 可跳过 / 只看部分",
  "uncertainties_or_risks": ["需要注意的不确定性或风险"],
  "disclaimer": "该摘要仅用于信息整理，不构成投资建议"
}}

视频文本：
{text_for_model}
"""
    models = [model] + [m for m in summary_config.get("fallback_models", []) if m and m != model]
    last_error: Optional[Exception] = None
    try:
        for candidate_model in models:
            try:
                data = _call_openai_json(client, candidate_model, system_prompt, user_prompt)
                summary_html = _render_structured_digest(video, content_source, data, transcript_note)
                return SummaryResult(status="success", model=candidate_model, summary_html=summary_html)
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(str(last_error))
    except Exception as exc:
        html_text = render_link_only_digest(
            video,
            f"LLM summary failed: {exc}. The original video link is provided.",
            content_source=content_source,
        )
        return SummaryResult(status="failed", model=model, summary_html=html_text, error_message=str(exc))
