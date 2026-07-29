"""The application layer: one entry point for every interface (phase 09).

plan/09 → *Application service layer: the single entry point both API and CLI
call; issues commands to the workflow engine, never re-implements transition
rules.*

Everything a person can ask groundscribe to do arrives here, whether it came
over HTTP or from a terminal. The interfaces above translate arguments and
render results; they hold no editorial or workflow logic, because two copies of
a rule are two rules.
"""

from __future__ import annotations
