# Decisions

1. Deterministic core, no runtime LLM dependency. Every decision carries a
   scored factor list and evidence references, so "why?" is always answerable.
2. Everything is normalised to a canonical Observation before analysis.
   Only the parser layer is expected to change on event day.
3. Template masking (uuid/ip/hex/ts/url/path/number) instead of hand-written
   per-dataset regexes, so unknown message shapes still group.
4. Python + Streamlit + pandas: one process, no frontend/backend split, no
   build step. Lowest risk for a 3-hour sprint. Generic core in
   src/signal_sprint/, case-specific overrides only in scenario/.
5. Optional Claude bridge: copy an incident's evidence bundle as a prompt,
   paste the narrative back. Uses the sanctioned tool without an API key.
