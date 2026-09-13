# Decisions

1. Deterministic core, no runtime LLM dependency. Every decision carries a
   scored factor list and evidence references, so "why?" is always answerable.
2. Everything is normalised to a canonical Observation before analysis.
   Only the parser layer is expected to change on event day.
3. Drain-style template mining instead of hand-written regexes, so unknown
   message shapes still group.
4. Scoring weights live in one file (rank/scoring.py) so they can be tuned
   live during the sprint.
5. Optional Claude bridge: copy an incident's evidence bundle as a prompt,
   paste the narrative back. Uses the sanctioned tool without an API key.
