from axiom.provider_gateway.client import GatewayLlmClient, ProviderGatewayError

__all__ = [
    "CircuitState",
    "GatewayLlmClient",
    "ProviderGateway",
    "ProviderGatewayError",
    "ProviderGatewayHttpServer",
    "ProviderGatewayRequestError",
]


def __getattr__(name: str):
    if name in {
        "CircuitState",
        "ProviderGateway",
        "ProviderGatewayHttpServer",
        "ProviderGatewayRequestError",
    }:
        from axiom.provider_gateway import controller

        return getattr(controller, name)
    raise AttributeError(name)
