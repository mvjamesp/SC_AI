"""The switched Codex route: its header, the local answer on it, and readiness.

Codex desktop 26.917.51856 and later disables Send for every model once the
ChatGPT allowance runs out, but only while its active provider needs OpenAI
sign-in. The active-limit Switch Codex fallback therefore needs no sign-in.
Normal OpenRouter-only use keeps the ChatGPT account for projects and setup
state. Both variants send `HEADER` with every request, so a subscription turn
is refused locally instead of failing upstream after retries. The header
marks the route; a missing credential never does.

On that route a ChatGPT request is answered here and nothing is sent. Native
service paths get the no-subscription answers from `local_services`. Every
other request gets one final `invalid_prompt` refusal that names the model's
twin row when this machine's Codex picker lists it. `ingress.error` sends it
to a streaming turn as one `response.failed` frame, whose message Codex
0.153.1 and 0.158 show as sent, and to anything else as a 400; neither
version retries either form, and 0.153.1 shows the 400's whole body.

Metadata helpers resolve to the external Luna row before adapter selection.
Ordinary subscription turns keep the refusal below.

`ready` is what Switch Codex asks before it points Codex's picker at the
routed-only list (`routed_models`). The check lives here rather than beside
the list's writer because the availability sync imports that writer, and the
check reads availability back: side by side they would import each other.
"""
from . import (availability, codex_helpers, errors, local_services, provider_defs, routed_models,
               subscription_roster)
from .clients import visible_for

HEADER = codex_helpers.ROUTE_HEADER


class Refusal(errors.LocalAnswer):
    """One final answer; `stream` asks for a `response.failed` frame."""

    def __init__(self, message, stream, detail=None):
        super().__init__(400, 'invalid_prompt', message, detail)
        self.stream = stream


def switched(headers):
    """True when the request carries `HEADER`, whatever its case."""
    return codex_helpers.switched(headers)


def message(twin=None):
    """Refusal wording that names `twin`, a registry row, when there is one."""
    login = provider_defs.OPENAI.subscription_label
    routed = provider_defs.label_for(provider_defs.DEFAULT_EXTERNAL)
    if twin is None:
        subject, pick = 'This model', 'Pick another model'
    else:
        label = provider_defs.label_for(twin.provider)
        # A picker label reads `<Vendor>: <Model> · <Provider>` (AGENTS.md section 8).
        subject = twin.display_name.removesuffix(' · ' + label).removeprefix(twin.vendor + ': ')
        pick = f'Pick "{twin.display_name}" to use it through {label}'
    return (f'{subject} needs your {login}, which is unavailable on the '
            f'{routed}-only connection. {pick}, or open Switch Codex to go back to your {login}.')


def refuse(path, payload, context, twin=None):
    """Raise the local answer for a ChatGPT request on the switched route.

    `twin` is the twin slug this machine may route, already gated by the
    portco allowlist and the key's availability; the answer names it only
    when it is this model's own twin (never the Luna stand-in that answers
    helper turns) and the Codex picker lists its row.
    """
    if provider_defs.native_service_path(path):
        local_services.answer(path)
    own = subscription_roster.TWIN_PREFIX + subscription_roster.SUCCESSORS.get(
        context.slug, context.slug)
    row = context.catalogue.get(twin) if twin == own else None
    if row is not None and not visible_for(row, 'codex'):
        row = None
    raise Refusal(message(row), payload.get('stream') is True,
                  f'switched Codex route: {context.slug}, twin {row.slug if row else None}')


def ready(home):
    """True when the routed-only list has rows the key's current snapshot allows.

    "Current" is the id set the picker merge reads (`availability.effective_ids`
    for the installed key and region). A list that still names a model that
    snapshot no longer allows fails until the next sync rewrites it.
    """
    allowed = availability.effective_ids(
        home, availability.current_key(home), availability.current_region(home))
    listed = routed_models.rows(home)
    slugs = {row.get('slug') for row in listed}
    return bool(listed) and allowed is not None and all(
        codex_helpers.served_slug(row) in allowed
        and (row.get('slug') not in codex_helpers.ALIASES
             or codex_helpers.LUNA_TWIN in slugs) for row in listed)
