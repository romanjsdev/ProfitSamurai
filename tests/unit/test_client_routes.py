from fastapi.routing import APIRoute

from profitsamurai.api.routers.clients import router


def test_client_routes() -> None:
    paths = {route.path for route in router.routes if isinstance(route, APIRoute)}
    assert paths == {
        "/clients",
        "/clients/{client_id}",
        "/clients/{client_id}/provision",
        "/clients/{client_id}/shops",
    }
