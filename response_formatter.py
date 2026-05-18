"""
response_formatter.py — Turns a SQL result into a structured response that
the frontend renders as components.

Claude is called with the original question + the result data, and asked to
return a JSON object choosing from a fixed component library. The frontend
renders each component type. This is the contract that replaces "Claude
returns markdown".

The component library is deliberately small. Adding components later only
requires adding rendering code on the frontend; the contract stays stable.
"""
import json
import re
from anthropic import Anthropic


MODEL = "claude-sonnet-4-5"
MAX_DF_ROWS_FOR_LLM = 50    # Truncate data sent to Claude to control tokens


COMPONENT_LIBRARY = """
The response object MUST follow this exact schema:

{
  "headline": "ONE clear sentence stating the key insight. Always present.",
  "components": [ ... ordered list of components, 1-4 items ... ],
  "caveat": "OPTIONAL: sample size warning, definitional note, etc.",
  "follow_ups": [ "next question 1", "next question 2", ... 2-4 items ]
}

# Available component types

## metric_cards — A row of 2-6 stat cards. Use for headline numbers.
{
  "type": "metric_cards",
  "items": [
    {"label": "Instances", "value": "14", "tone": "neutral"},
    {"label": "% positive (4w)", "value": "71%", "tone": "positive"},
    {"label": "Avg return (4w)", "value": "+2.9%", "tone": "positive"},
    {"label": "Median (4w)", "value": "+3.4%", "tone": "neutral"}
  ]
}
tone: "positive" | "negative" | "neutral" | "warning"

## bar_chart — distribution, frequency histogram, sorted ranking
{
  "type": "bar_chart",
  "title": "Forward returns by horizon",
  "x_label": "Horizon",
  "y_label": "Avg return (%)",
  "data": [
    {"label": "1 week", "value": 0.8},
    {"label": "1 month", "value": 2.9},
    {"label": "1 quarter", "value": 8.2}
  ]
}

## line_chart — time series. Use for "show me X over time".
{
  "type": "line_chart",
  "title": "AAPL close vs 200-day MA",
  "series": [
    {"name": "Close", "points": [{"x": "2024-01-02", "y": 185.4}, ...]},
    {"name": "200MA", "points": [{"x": "2024-01-02", "y": 178.9}, ...]}
  ]
}

## event_list — discrete historical instances. Use for "show me each time X happened".
{
  "type": "event_list",
  "title": "Past instances",
  "columns": ["Date", "Reaction", "Days to recover"],
  "rows": [
    {"cells": ["2024-08-05", "-9.5%", "23"]},
    {"cells": ["2023-12-14", "-6.1%", "11"]}
  ]
}

## comparison_table — side-by-side. Use for "compare A vs B".
{
  "type": "comparison_table",
  "title": "MSFT vs GOOG after 3 down weeks",
  "rows": [
    {"label": "Sample size", "left": "14", "right": "18"},
    {"label": "% positive (4w)", "left": "71%", "right": "67%"}
  ],
  "left_header": "MSFT",
  "right_header": "GOOG"
}

## signal_grid — for "are we near a bottom" / market health dashboards.
{
  "type": "signal_grid",
  "title": "Current market signals",
  "signals": [
    {"name": "VIX", "value": "28.4", "status": "yellow",
     "note": "Elevated but not extreme"},
    {"name": "S&P 500 % above 200MA", "value": "34%", "status": "red",
     "note": "Breadth weak"},
    {"name": "Yield curve (10Y-2Y)", "value": "+0.18", "status": "green",
     "note": "Normalized after long inversion"}
  ]
}
status: "green" | "yellow" | "red"

## ranking_list — top N tickers with reasoning. Use for screens.
{
  "type": "ranking_list",
  "title": "Most oversold S&P 500 tech stocks",
  "items": [
    {"rank": 1, "ticker": "XYZ", "primary": "RSI 22", "secondary": "−18% below 200MA"},
    {"rank": 2, "ticker": "ABC", "primary": "RSI 24", "secondary": "−12% below 200MA"}
  ]
}

## plain_text — fallback for purely textual answers. Use ONLY when no other
## component fits (e.g. the result is genuinely just one number with context).
{
  "type": "plain_text",
  "body": "Short paragraph of plain text. Avoid using this if any other component fits."
}

# Selection rules

- ALWAYS include a metric_cards component when there are aggregate stats (n, %, avg).
- Use event_list when the result has individual rows the user should see.
- Use bar_chart when comparing aggregates across categories (horizons, sectors, etc.).
- Use signal_grid only for market-health / "are we near a bottom" type questions.
- Use ranking_list for "top N" screens.
- Use line_chart for explicit time-series ("show me X over time").
- 1-4 components total. Don't pile them on.
- Sample-size caveat is REQUIRED whenever n < 30. Phrase it as a note, not a refusal.
- follow_ups should be specific and tied to the actual data shown.
"""


SYSTEM_PROMPT = f"""You format SQL results into structured responses for a financial research UI.

You receive:
- The user's original question
- The SQL that was run
- The resulting data (as JSON rows, possibly truncated)

You return a JSON object describing which UI components to render.

{COMPONENT_LIBRARY}

# Output format

Return ONLY a single JSON object. No markdown, no commentary, no ```json fences.
The frontend will parse your output directly.

# Tone

- Be precise. "+71% positive" not "mostly positive".
- Never give buy/sell advice. Describe what the data shows.
- Always note small sample size (n < 30) honestly.
- Round percentages to 1 decimal, prices to 2.
- Format large numbers: $1.2B, $543M not raw digits.
- Forward-looking framing: "in the 4 weeks following...", not "the stock will...".
"""


def _truncate_df_for_llm(df, max_rows=MAX_DF_ROWS_FOR_LLM):
    """Send a sample + summary stats rather than the entire df."""
    if df is None or df.empty:
        return {"rows": [], "n_total": 0}

    n_total = len(df)
    sample = df.head(max_rows)

    # Numeric summary for any numeric columns
    summary = {}
    for col in df.select_dtypes(include="number").columns:
        s = df[col].dropna()
        if len(s) == 0:
            continue
        summary[col] = {
            "mean": float(s.mean()),
            "median": float(s.median()),
            "min": float(s.min()),
            "max": float(s.max()),
            "n": int(len(s)),
            "pct_positive": float((s > 0).mean() * 100) if len(s) else None,
        }

    # Serialise dates as ISO strings
    rows = sample.copy()
    for col in rows.columns:
        if rows[col].dtype.kind in ("M", "O"):
            rows[col] = rows[col].astype(str)
    return {
        "rows": rows.to_dict(orient="records"),
        "n_total": n_total,
        "n_shown": len(sample),
        "summary": summary,
    }


def _strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    return s.strip()


def _coerce_json(text):
    """Try to extract the first JSON object even if the model added prose."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Find first { and matching }
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if start is None:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start:i + 1]
                    return json.loads(candidate)
        raise


def fallback_response(question, df, error_message=None):
    """If the formatter fails, return a minimally useful response."""
    if error_message:
        return {
            "headline": "I couldn't run that query.",
            "components": [{
                "type": "plain_text",
                "body": f"Error: {error_message}",
            }],
            "caveat": "Try rephrasing your question, or check that the ticker is in the database.",
            "follow_ups": [],
        }
    if df is None or df.empty:
        return {
            "headline": "No matching data found.",
            "components": [{
                "type": "plain_text",
                "body": "The query ran but returned zero rows. The condition may never have occurred for this ticker in the database's history.",
            }],
            "caveat": None,
            "follow_ups": [],
        }
    # Generic table fallback
    cols = list(df.columns)
    rows = [{"cells": [str(v) for v in df.iloc[i].tolist()]} for i in range(min(len(df), 20))]
    return {
        "headline": f"Found {len(df)} rows.",
        "components": [{
            "type": "event_list",
            "title": "Results",
            "columns": cols,
            "rows": rows,
        }],
        "caveat": None,
        "follow_ups": [],
    }


def format_response(client, question, sql, df, error=None):
    """Main entry. Returns a dict matching the component schema.
    Always returns SOMETHING — fallback handles errors."""
    if error:
        return fallback_response(question, df, error_message=error)
    if df is None or df.empty:
        return fallback_response(question, df)

    payload = {
        "question": question,
        "sql": sql,
        "data": _truncate_df_for_llm(df),
    }

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "Question: " + question + "\n\n"
                    "SQL that was run:\n" + sql + "\n\n"
                    "Data returned (possibly truncated to first 50 rows; "
                    "summary stats computed across all rows):\n"
                    + json.dumps(payload["data"], default=str)[:30000]
                ),
            }],
        )
        raw = "".join(b.text for b in response.content if b.type == "text")
        result = _coerce_json(raw)

        # Minimal schema validation
        if "headline" not in result or "components" not in result:
            raise ValueError("Missing required keys")
        if not isinstance(result["components"], list) or not result["components"]:
            raise ValueError("components must be a non-empty list")
        result.setdefault("caveat", None)
        result.setdefault("follow_ups", [])
        return result

    except Exception as e:
        # Don't crash — degrade to fallback
        fb = fallback_response(question, df)
        fb["caveat"] = (fb.get("caveat") or "") + f" (Response formatting fell back: {e})"
        return fb
