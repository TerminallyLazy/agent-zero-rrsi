"""Read-only model-price suggestions; never dispatch model calls or expose secrets."""
import math
from helpers.api import ApiHandler, Input, Output, Request, Response

ROLES = ('policy', 'utility', 'vision', 'proposer', 'analyst', 'critic', 'digester', 'embedding')


def catalog_price(config, catalog, role):
    provider, name = str(config.get('provider') or ''), str(config.get('name') or '')
    unknown = {'rates': None, 'source': 'unavailable'}
    if not provider or not name:
        return {**unknown, 'reason': 'Choose a model in Agent Zero first.'}
    if any(word in provider.lower() for word in ('oauth', 'codex', 'copilot', 'subscription')):
        return {**unknown, 'reason': 'Subscription pricing needs your own cost policy.'}
    kwargs = config.get('kwargs') or {}
    if any(config.get(key) or kwargs.get(key) for key in ('api_base', 'base_url', 'api_base_url')):
        return {**unknown, 'reason': 'Custom endpoints require explicit prices.'}
    if provider == 'huggingface' and role == 'embedding':
        return {'rates': {'input_per_million': 0, 'output_per_million': 0}, 'source': 'local',
                'reason': 'Local embeddings have no provider token charge; hardware costs are excluded.'}
    for key in (f'{provider}/{name}', name):
        entry = catalog.get(key)
        if not isinstance(entry, dict) or (key == name and entry.get('litellm_provider') != provider):
            continue
        def maximum(prefix):
            values = [v for k, v in entry.items() if k == prefix or k.startswith(prefix + '_above_')]
            if not values or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
                return None
            return max(values) * 1_000_000
        inp, out = maximum('input_cost_per_token'), maximum('output_cost_per_token')
        if role == 'embedding' and out is None:
            out = 0
        reasoning = entry.get('output_cost_per_reasoning_token')
        if reasoning is not None:
            if type(reasoning) not in (int, float) or not math.isfinite(reasoning) or reasoning < 0:
                continue
            if out is not None:
                out = max(out, reasoning * 1_000_000)
        if inp is not None and out is not None:
            return {'rates': {'input_per_million': inp, 'output_per_million': out}, 'source': 'catalog',
                    'reason': 'Installed LiteLLM catalog estimate; review current provider rates before saving.'}
    return {**unknown, 'reason': 'No matching token prices in the installed catalog.'}


def suggestions():
    import litellm
    from helpers import plugins
    from plugins._model_config.helpers.model_config import (
        get_chat_model_config, get_utility_model_config, get_vision_model_config, get_embedding_model_config)
    chat = get_chat_model_config() or {}
    configs = {role: dict(chat) for role in ROLES if role != 'embedding'}
    configs['utility'] = get_utility_model_config() or chat
    configs['vision'] = get_vision_model_config() or chat
    configs['embedding'] = get_embedding_model_config() or {}
    for role, override in (plugins.get_plugin_config('rrsi') or {}).get('model_roles', {}).items():
        if role in configs and isinstance(override, dict):
            configs[role] = {**configs[role], **override}
    catalog = getattr(litellm, 'model_cost', {})
    return {'roles': {role: {'provider': str(cfg.get('provider') or ''), 'model': str(cfg.get('name') or ''),
                            **catalog_price(cfg, catalog, role)} for role, cfg in configs.items()},
            'source': 'Installed Agent Zero / LiteLLM catalog', 'model_calls': 0}


class Pricing(ApiHandler):
    async def process(self, input: Input, request: Request) -> Output:
        if request.method != 'POST':
            return Response('Method not allowed', 405)
        try:
            return {'success': True, 'data': suggestions()}
        except Exception:
            return {'success': False, 'error': 'Model prices are unavailable. You can enter rates manually.'}
