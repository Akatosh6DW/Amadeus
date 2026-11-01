"""Render the frozen reply layers identically on fast and tool-capable paths."""
from agent.reply_prompt_layers import EXPRESSION_EMOTION_RULE
from backend.context.prompt_assembly import system_message


def reply_layer_messages(snapshot, natural_style: str, emotion_rule: str, *, visual: bool = False):
    layers = snapshot.reply_layers
    if layers is not None:
        result = [system_message(name, content, source='reply_layers', category=category)
                  for name, category, content in layers.sections() if content]
        result.append(system_message('persona.emotion',
                      EXPRESSION_EMOTION_RULE if layers.independent else emotion_rule,
                      source='reply_layers', category='expression'))
        return result
    return [
        *([] if visual else [system_message('persona.core', snapshot.system_prompt,
                                            source='reply_layers', category='persona')]),
        system_message('persona.style', natural_style, source='reply_layers', category='expression'),
        *([] if visual else [system_message('persona.skills', snapshot.prompt_text,
                                            source='reply_layers', category='persona')]),
        system_message('persona.emotion', emotion_rule, source='reply_layers', category='persona'),
    ]


def independent_persona(snapshot) -> bool:
    return bool(snapshot is not None and snapshot.reply_layers and snapshot.reply_layers.independent)
