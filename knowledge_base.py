"""Curated, local Uhhhhhh migration guidance. Not model training or verified fixes."""
from __future__ import annotations
import re

# Guidance is manually curated, not scraped from uploaded code or model output.
# Links identify references for human review, not permission to execute code.
ARTICLES = (
    {'id':'hub-contract', 'triggers':(), 'source':'https://raw.githubusercontent.com/STEVE-916-create/Uhhhhhh/main/source/reanim.lua', 'text':'Uhhhhhh expects a returned array of module factory functions; MOVESET tables include Name, Description, Assets, Config, Init, Update and Destroy. The hub supplies figure to Init/Update; Destroy may receive nil. ContextActions is injected into the factory environment. These facts do not establish runtime correctness.'},
    {'id':'lerp-euler', 'triggers':('lerp','weld.c0','weld.c1'), 'source':'provided KyusEnder.lua.txt, Lerp function', 'text':'Interpolation formulas are source-specific. KyusEnder defines Euler-component interpolation. lerpPoseEuler exists in this builder, but must only be used if source formula, C0/C1 state, alpha, and update cadence are understood. Different clerp functions must not be assumed equivalent.'},
    {'id':'quaternion', 'triggers':('quaternion','clerp'), 'source':'provided Spectrum Glitcher source, clerp and QuaternionSlerp definitions', 'text':'Spectrum Glitcher defines clerp using quaternion helpers. Euler-component interpolation is not guaranteed equivalent. Do not replace its clerp with lerpPoseEuler merely to remove a warning; preserve the original algorithm or mark it unresolved.'},
    {'id':'async-lifecycle', 'triggers':('while ', 'repeat', 'connect(', ':connect('), 'source':'https://raw.githubusercontent.com/STEVE-916-create/Uhhhhhh/main/source/reanim.lua', 'text':'Connections, tasks and loops require lifecycle ownership. The hub may call Destroy with nil. Preserving cancellation and cleanup requires checking the specific code; do not remove loops or claim cleanup is implemented without evidence.'},
    {'id':'external-code', 'triggers':('loadstring', 'getobjects', 'httpget', 'require('), 'source':'https://luau.org/library', 'text':'Dynamically loaded code or assets are not contained in the source. Do not execute, silently fetch, invent, or assume the behavior of external dependencies. Identify the dependency and request its source if needed.'},
    {'id':'preserve-behavior', 'triggers':('effect', 'cfuncs', 'createpart', 'sphere'), 'source':'provided example scripts; manual review required', 'text':'Effects and helper definitions are source-specific. A similarly named generic helper may have different behavior, timing or cleanup. Quote and preserve relevant original expressions and list missing dependencies; do not replace unknown effects with generic approximations.'},
    {'id':'input', 'triggers':('keydown', 'inputbegan','button1down','contextactions'), 'source':'https://raw.githubusercontent.com/STEVE-916-create/Uhhhhhh/main/source/reanim.lua', 'text':'Map input from observed source, not guesses. ContextActions is provided by the hub and actions must be unbound. A mouse-based ability may require targeting context that the current builder does not provide.'},
)

def retrieve(source: str, max_chars: int = 4500):
    lower = source.lower()
    found = [a for a in ARTICLES if not a['triggers'] or any(trigger in lower for trigger in a['triggers'])]
    result=[]; used=0
    for a in found:
        entry={k:a[k] for k in ('id','source','text')}
        if used + len(entry['text']) > max_chars: continue
        used += len(entry['text']); result.append(entry)
    return result
