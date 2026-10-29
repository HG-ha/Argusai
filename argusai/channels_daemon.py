"""Headless communication-channel host using the same runtime as the GUI.

Compatibility shim: prefer ``argusai channels serve``.
"""

from __future__ import annotations


def main() -> None:
    from argusai.cli.commands.channels import run_channels_serve

    run_channels_serve()


if __name__ == "__main__":
    main()
