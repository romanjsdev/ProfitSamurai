from fastapi.routing import APIRoute

from profitsamurai.api.routers.cabinets import router


def test_cabinet_routes() -> None:
    paths = {route.path for route in router.routes if isinstance(route, APIRoute)}
    assert paths == {"/cabinets", "/cabinets/{business_id}"}
