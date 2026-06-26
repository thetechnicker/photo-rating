from .main import app

__all__ = ["app"]


def main():
    from uvicorn import run

    run(app)
