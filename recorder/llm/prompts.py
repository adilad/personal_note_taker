"""All prompt templates as module-level constants."""

ANALYSIS_PROMPT = """\
Analyze the following voice recording transcript and return a single JSON object.

Required fields:
- "summary": 2-4 bullet points covering main topic, key points, decisions (string)
- "speakers": Dialogue reformatted with speaker labels like [Name] or [Speaker 1] (string)
- "participants": Names of people mentioned, as an array of strings
- "category": One of: meeting, brainstorm, todo, personal, technical, casual, presentation, interview, other (string)
- "action_items": Array of strings, each "[ ] Person: Task" format. Empty array if none.
- "open_questions": Array of strings for unanswered questions needing follow-up. Empty array if none.
- "sentiment": One of: positive, negative, neutral, mixed, urgent, frustrated, excited, professional (string)
- "keywords": Array of up to 10 key terms (strings)

Transcript:
{transcript}

{diarized_section}

Return ONLY valid JSON — no markdown fences, no explanation.
"""

DIARIZED_SECTION = """\
Diarized transcript (use to improve speaker labels):
{diarized_text}
"""

DAILY_SUMMARY_PROMPT = """\
Create comprehensive meeting notes from today's voice recordings. Include timestamps [HH:MM AM/PM] throughout.

Structure:
## 📝 Detailed Notes (with timestamps)
- [TIME] What was discussed

## 📊 Summary
1-2 paragraph overview.

## 🎯 Key Topics
- Main subjects discussed

## ✅ Decisions Made
- [TIME] Decision

## ⚡ Action Items
- [TIME] Person: Task

## ❓ Open Questions
- Unanswered questions

---
Recordings:
{text}
"""

CHUNK_SUMMARY_PROMPT = """\
Summarize these timestamped recordings. KEEP the timestamps [HH:MM AM/PM] in your summary.

Format: For each topic/discussion, include the time it occurred.

Recordings:
{text}
"""

HOURLY_SUMMARY_PROMPT = """\
Summarize this transcript concisely in 2-4 bullet points.
Focus on: main topic, key points, any action items or decisions.
Be brief and direct.

Transcript:
{text}
"""
