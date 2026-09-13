# Decisions

1. Deterministic core, no runtime LLM dependency. Every decision carries a
   scored factor list and evidence references, so "why?" is always answerable.
2. Everything is normalised to a canonical Observation before analysis.
   Only the parser layer is expected to change on event day.
3. Template masking (uuid/ip/hex/ts/url/path/number) instead of hand-written
   per-dataset regexes, so unknown message shapes still group.
4. Everything lives in one file (signal_sprint.py), standard library only:
   nothing to install on event day, one place to read and tune. Scoring
   weights are a single dict (WEIGHTS).
5. Optional Claude bridge: copy an incident's evidence bundle as a prompt,
   paste the narrative back. Uses the sanctioned tool without an API key.
