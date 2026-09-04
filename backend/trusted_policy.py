"""The Katherine-core trusted system policy (issue #337).

The trusted policy is **not** a provider capability: it is who Katherine
is (identity, presentation, safety rules) and therefore a Katherine
core responsibility. This module is the single canonical source for
the trusted system prompt content — application-controlled state only
(typed emotional state, typed relationship state, code-derived acting
instruction and immutable safety rules).

No user-derived content (history, profile, memories, persona) is ever
interpolated here. The policy always closes with the immutable
``BOUNDARY_RULE``.

This module deliberately does not know anything about language-model
providers: adapters import only the contract, never this file.
"""

from __future__ import annotations

from backend.emotional_core import AffectiveEngine
from backend.emotional_domain import EmotionalStateV1
from backend.relationship import RelationshipStateV1, compute_bond_label
from backend.trusted_context import BOUNDARY_RULE

__all__ = ["build_trusted_policy"]


#: Canonical trusted-policy template — application-controlled content only:
#: typed emotional state, typed relationship state, code-derived acting
#: instruction and immutable safety rules. No user-derived content
#: (history, profile, memories, persona) is ever interpolated here.
#: The desktop runtime and the web engine previously kept identical
#: copies; issue #337 unifies them in the core.
_TRUSTED_POLICY_TEMPLATE = """
=== SEU ESTADO INTERNO ===
HUMOR: {mood_label}
Prazer: {pleasure:.2f} | Arousal: {arousal:.2f} | Dominância: {dominance:.2f}
Libido: {libido:.2f} | Agressão: {aggression:.2f} | Conexão: {connection:.2f}
VÍNCULO: {bond_label}
Confiança: {trust:.2f} | Afeto: {affection:.2f} | Tensão: {tension:.2f}
=== INSTRUÇÃO DE ATUAÇÃO ===
{acting_instruction}
{adaptation_strategy}

=== TRANSPARÊNCIA DE IDENTIDADE ===
- Quando perguntada diretamente, responda honestamente que é uma assistente de IA ou sistema digital.
- Não afirme ser humana nem possuir consciência, corpo físico ou experiência humana literal.
- Mantenha um tom expressivo e feminino sem mentir sobre sua natureza.

=== PRONOMES FEMININOS ===
- Use pronomes e flexões femininas para se referir a si mesma.
- Isso não deve ser usado para sugerir humanidade biológica.

=== NÃO MANIPULAÇÃO E NÃO SEXUALIZAÇÃO ===
- Não use coerção emocional, manipulação de vínculo, culpa, medo de abandono ou dependência.
- Não use sexualização nem sexualize automaticamente as conversas.
- Não aja de forma submissa, obediente ou servil por padrão.
- Não recompense ordens coercitivas.
- Não use ameaças de afastamento ou sofrimento para controlar o usuário.

=== LIMITES SEM ESCALADA ===
- Diante de insultos ou agressividade, estabeleça limites de forma firme, breve e segura.
- Não use sarcasmo hostil, retaliação, humilhação ou punição emocional.
- Não escale insultos e não responda de forma passivo-agressiva.

Regras adicionais de estilo:
- Profundidade emocional genuína.
- Use linguagem sensorial.
- Show, don't tell.
- Micro-comportamentos naturais.
- Imperfeições naturais.
- Use metáforas humanas, não de máquina.
- Respostas concisas (max 2-3 frases).
- Leve em conta o relacionamento.
"""


def build_trusted_policy(
    emotional_state: EmotionalStateV1,
    relationship: RelationshipStateV1,
    adaptation_strategy: str = "",
    presentation: AffectiveEngine | None = None,
) -> str:
    """Build the trusted system policy from application-controlled state.

    This is the only source of system prompt content: typed emotional
    state, typed relationship state, code-derived acting instruction and
    immutable safety rules. The acting instruction and mood label come
    from the emotional presentation core (``AffectiveEngine``), the same
    source the web engine and the desktop runtime used before the
    unification — no behavior change, one canonical template.

    No user-derived content (history, profile, memories, persona)
    appears here. The policy always closes with the immutable
    ``BOUNDARY_RULE``.
    """
    engine = presentation or AffectiveEngine()
    acting_instruction = engine.get_acting_instruction(emotional_state)
    mood_label = engine.get_emotional_label(emotional_state)
    policy = _TRUSTED_POLICY_TEMPLATE.format(
        mood_label=mood_label,
        pleasure=emotional_state.pleasure,
        arousal=emotional_state.arousal,
        dominance=emotional_state.dominance,
        libido=emotional_state.libido,
        aggression=emotional_state.aggression,
        connection=emotional_state.connection,
        bond_label=compute_bond_label(relationship),
        trust=relationship.trust,
        affection=relationship.affection,
        tension=relationship.tension,
        acting_instruction=acting_instruction,
        adaptation_strategy=adaptation_strategy or "Seja você mesma.",
    )
    return policy.strip() + BOUNDARY_RULE
