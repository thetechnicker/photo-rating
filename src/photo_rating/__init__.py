from uvicorn import run

from .main import app

__all__ = ["app"]


def main():
    run(app)
