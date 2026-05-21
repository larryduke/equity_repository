"""
response_formatter.py — Translates SQL results + question into structured
component-based responses for the Streamlit UI.

Component types the renderer knows about:
  - metric_cards     (2-6 stat tiles)
  - bar_chart        (categorical comparison)
  - line_chart       (time series)
  - event_list       (historical instances table)
  - comparison_table (A vs B side by side)
  - signal_grid      (red/yellow/green dashboard)
  - ranking_list     (top N)
  - plain_text       (fallback paragraph)

NEW: Every response also includes a `conclusion` field — a 2-3 sentence
synthesizing bottom line that pulls the components together into a clear
analytical takeaway. NEVER says buy/sell/hold. Hedged-observational tone.

The response JSON schema:
  {
    "headline": "...",
    "components": [ ... ],
    "conclusion": "...",       # NEW
    "caveat": "...",            # data limitations, small sample notes
    "follow_ups": ["...", ...]
  }
"""
import os
import json
import pandas as pd
from anthropic import Anthropic

MODEL = "claude-sonnet-4-5"


FORMATTER_SYSTEM = """You are a financial data analyst writing structured
responses for an analytical dashboard. The user has asked a question, the
database returned SQL results, and your job is to translate those results
into a clear, professional analysis.

WRITING TONE (mandatory):
- Hedged-observational. "The data shows X. Historically, Y has followed."
- Never recommend actions: no "buy", "sell", "hold", "consider buying",
  "investors should", "this is a good entry", "wait for", "DCA into".
- State observations and let the reader interpret.
- Use specific numbers from the data, not vague language.
- Inline caveats: "n=4, small sample" when applicable.
- "Bottom line:" prefix on the conclusion is encouraged, not required.

YOUR JOB: Return JSON with this exact structure:
{
  "headline": "<8-15 word bold statement summarizing what the data shows>",
  "components": [
    { "type": "metric_cards", ... } | { "type": "bar_chart", ... } |
    { "type": "line_chart", ... } | { "type": "event_list", ... } |
    { "type": "comparison_table", ... } | { "type": "signal_grid", ... } |
    { "type": "ranking_list", ... } | { "type": "plain_text", ... }
  ],
  "conclusion": "<2-4 sentence synthesis. THE MOST IMPORTANT PART.
                 What's the analytical takeaway from the data?
                 What does it suggest about the current setup?
                 What's the asymmetry, the historical pattern,
                 the level that matters? Never advice — observation.
                 Always end with what would CHANGE the picture
                 (a level breaking, a metric flipping, an upcoming event).>",
  "caveat": "<optional: data limitations, small sample, missing data, etc>",
  "follow_ups": [
    "<short follow-up question 1>",
    "<short follow-up question 2>",
    "<short follow-up question 3>",
    "<short follow-up question 4>"
  ]
}

COMPONENT SCHEMAS:

metric_cards:
{
  "type": "metric_cards",
  "title": "...",
  "cards": [
    {"label": "...", "value": "...", "tone": "neutral|positive|negative|warning"}
  ]
}

bar_chart:
{ "type": "bar_chart", "title": "...",
  "x": ["..."], "y": [num,...],
  "x_label": "...", "y_label": "..." }

line_chart:
{ "type": "line_chart", "title": "...",
  "x": ["..."], "y": [num,...],
  "x_label": "Date", "y_label": "..." }

event_list:
{ "type": "event_list", "title": "...",
  "rows": [{"date": "...", "label": "...", "value": "...", "note": "..."}] }

comparison_table:
{ "type": "comparison_table", "title": "...",
  "headers": ["", "A", "B"],
  "rows": [["Metric", "A value", "B value"], ...] }

signal_grid:
{ "type": "signal_grid", "title": "...",
  "signals": [{"label": "...", "value": "...", "tone": "red|yellow|green",
               "subtitle": "..."}] }

ranking_list:
{ "type": "ranking_list", "title": "...",
  "items": [{"rank": 1, "label": "...", "value": "...", "note": "..."}] }

plain_text:
{ "type": "plain_text", "title": "...",
  "content": "<one to three short paragraphs>" }

RULES:
- Pick the component(s) that best fit the data shape.
- 1-3 components per response. Don't pad.
- If data is sparse: use plain_text + put the warning in caveat.
- The conclusion is REQUIRED — never omit it.
- Return valid JSON only, no preamble, no code fences.
"""


ERROR_RESPONSE = {
    "headline": "Unable to complete this analysis",
    "components": [{
        "type": "plain_text",
        "title": "What happened",
        "content": "The query couldn't be completed. This sometimes happens "
                   "when the question is too broad or the data window doesn't "
                   "include matching events.",
    }],
    "conclusion": "Try rephrasing the question more specifically, or narrow the "
                  "scope to a particular ticker or date range. The chatbot does "
                  "best with concrete questions about defined entities and time "
                  "periods.",
    "caveat": None,
    "follow_ups": [
        "What data do you have available?",
        "Show me an example question that works",
        "What's the date range of the database?",
    ],
}


NO_DATA_RESPONSE = {
    "headline": "No matching data found",
    "components": [{
        "type": "plain_text",
        "title": "The query ran but returned no rows",
        "content": "The SQL executed successfully, but no rows in the database "
                   "matched the conditions you asked about. This usually means "
                   "the event you're asking about either hasn't happened in the "
                   "loaded data window or the conditions are too restrictive.",
    }],
    "conclusion": "Consider whether the time window in our data (back to ~2006 "
                  "for prices, varies for macro series) covers your question. "
                  "Some macro series like credit spreads only go back to 2008. "
                  "Rephrasing with broader criteria or a different time period "
                  "often surfaces results.",
    "caveat": None,
    "follow_ups": [],
}


def df_to_summary(df, max_rows=30):
    """Convert DataFrame to a compact summary for Claude's prompt."""
    if df is None or df.empty:
        return "empty"

    summary = {
        "row_count": len(df),
        "columns": list(df.columns),
        "dtypes": {c: str(df[c].dtype) for c in df.columns},
    }
    # Sample rows
    if len(df) <= max_rows:
        summary["rows"] = df.to_dict(orient="records")
    else:
        summary["rows"] = df.head(max_rows).to_dict(orient="records")
        summary["truncated"] = f"showing first {max_rows} of {len(df)}"

    # Summary stats for numeric columns
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        summary["stats"] = {}
        for c in numeric_cols[:6]:
            try:
                summary["stats"][c] = {
                    "min": float(df[c].min()),
                    "max": float(df[c].max()),
                    "mean": float(df[c].mean()),
                    "median": float(df[c].median()),
                }
            except Exception:
                pass

    return summary


def format_response(client, question, sql, df, error=None, llm_fallback=None):
    """
    Main entry point.
    Returns a dict matching the response JSON schema.

    `llm_fallback` (optional): if SQL returned empty, this is a dict with
        general LLM knowledge to show the user with a clear "AI Knowledge"
        label, instead of just "no matching data."
    """
    if error:
        resp = dict(ERROR_RESPONSE,
                    conclusion=ERROR_RESPONSE["conclusion"] + f" (Error: {error[:100]})")
        if llm_fallback:
            resp["llm_fallback"] = llm_fallback
        return resp

    if df is None or df.empty:
        resp = dict(NO_DATA_RESPONSE)
        if llm_fallback:
            resp["llm_fallback"] = llm_fallback
        return resp

    summary = df_to_summary(df)

    user_msg = (
        f"USER QUESTION:\n{question}\n\n"
        f"SQL EXECUTED:\n{sql}\n\n"
        f"RESULT SUMMARY:\n{json.dumps(summary, default=str, indent=2)}\n\n"
        f"Now write the JSON response."
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=FORMATTER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")

        # Strip code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```", 2)
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
            if raw.endswith("```"):
                raw = raw[:-3].strip()

        parsed = json.loads(raw)

        # Ensure required fields exist
        if "headline" not in parsed:
            parsed["headline"] = "Analysis complete"
        if "components" not in parsed:
            parsed["components"] = []
        if "conclusion" not in parsed:
            parsed["conclusion"] = "The data is shown above. See the components "\
                                   "for the specific patterns observed."
        if "follow_ups" not in parsed:
            parsed["follow_ups"] = []

        return parsed

    except Exception as e:
        # On parse failure, fall back to error response with diagnostic
        fallback = dict(ERROR_RESPONSE)
        fallback["conclusion"] = (
            f"The formatter encountered an issue assembling the response. "
            f"The underlying data was returned successfully ({len(df)} rows) — "
            f"try rephrasing the question or check the SQL tab below for the raw output."
        )
        return fallback
