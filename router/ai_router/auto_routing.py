"""The two OpenRouter adaptive routes choose their own model and effort."""
import re

from . import provider_defs

SLUGS = frozenset(('openrouter/auto', 'openrouter/auto-code'))


def is_auto(slug):
    return isinstance(slug, str) and slug in SLUGS


def accepts_model(expected, reported):
    """A routed selection is evidence of selection, not a fixed-model match."""
    return reported == expected or (is_auto(expected) and isinstance(reported, str)
        and re.fullmatch(r'[~A-Za-z0-9][A-Za-z0-9._/+:~\-]{0,127}', reported) is not None)


def project(payload, slug):
    """Let the adaptive engine choose effort; preserve every tool and message."""
    if not is_auto(slug):
        return payload
    result = {key: value for key, value in payload.items() if key != 'thinking'}
    for key in ('reasoning', 'output_config'):
        value = result.get(key)
        if isinstance(value, dict) and 'effort' in value:
            value = {name: item for name, item in value.items() if name != 'effort'}
            if value:
                result[key] = value
            else:
                result.pop(key)
    return result


def restrict(payload, context, allowed):
    """A managed adaptive route may select only approved OpenRouter rows."""
    if allowed is None or not is_auto(context.slug):
        return payload
    models = sorted({row.upstream_id or row.slug for slug, row in context.catalogue.items()
                     if slug in allowed and row.provider == provider_defs.OPENROUTER.id
                     and not is_auto(slug)
                     and row.media_kind is None})
    if not models:
        raise ValueError('Automatic routing requires approved underlying OpenRouter models')
    plugins = payload.get('plugins', [])
    if not isinstance(plugins, list):
        raise ValueError('plugins must be a list')
    plugins = [plugin for plugin in plugins if not (
        isinstance(plugin, dict) and plugin.get('id') == 'auto-router')]
    return {**payload, 'plugins': plugins + [{'id': 'auto-router', 'allowed_models': models}]}
