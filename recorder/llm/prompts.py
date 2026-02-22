"""All prompt templates as module-level constants."""

ANALYSIS_PROMPT = """\
Analyze this voice recording transcript and return a single JSON object.

Rules:
- "summary": Main topic on the first line, then 2-3 short bullet points (•) covering key points or decisions. Max 4 lines total. No padding.
- "speakers": Reformat as dialogue with labels [Name] or [Speaker 1]. Empty string if single speaker.
- "participants": Names of people explicitly mentioned. Empty array if none.
- "category": One of: meeting, brainstorm, todo, personal, technical, casual, presentation, interview, other
- "action_items": Only concrete tasks with an owner. Format "[ ] Person: Task". Empty array if none.
- "open_questions": Only questions that genuinely need follow-up. Empty array if none.
- "sentiment": One of: positive, negative, neutral, mixed, urgent, frustrated, excited, professional
- "keywords": Up to 5 key terms. Empty array if nothing substantive.

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
Summarize today's voice recordings. Be concise.

Format (use exactly this structure, omit any section that has nothing to add):

**[Main topic of the day]**
• Key point or event — include timestamp [H:MM AM/PM]
• Key point or event — include timestamp
• Key point or event — include timestamp

**Action Items** (omit if none)
• [TIME] Person: Task

Do not add introductory sentences, do not pad, do not repeat yourself.

Recordings:
{text}
"""

CHUNK_SUMMARY_PROMPT = """\
Summarize these recordings as short bullet points. Keep timestamps [HH:MM AM/PM].
One bullet per distinct topic. No filler.

Recordings:
{text}
"""

HOURLY_SUMMARY_PROMPT = """\
2-3 bullet points max. Main topic first, then key points or decisions.
If nothing substantive happened, return a single bullet saying so.

Transcript:
{text}
"""
