from .generic import GenericAdapter

REGISTRY = {'generic': GenericAdapter}


def get_adapter(name, cfg):
    cls = REGISTRY.get(name)
    if cls is None:
        raise RuntimeError(f"adaptador '{name}' não existe (disponíveis: {', '.join(sorted(REGISTRY))})")
    return cls(cfg)
