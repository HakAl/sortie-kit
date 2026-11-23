"""Run this toolkit by the path of this file: `PYTHON -P .../sortie_kit/entry.py ARGS`.

The host gives each seat that command. A seat runs it from the target
repository and with the developer's environment, and `PYTHON -m sortie_kit`
would there take whichever `sortie_kit` the working directory or PYTHONPATH
offers first. Named by its file, the package that runs is the one beside
this file, the one the host itself loaded, wherever the command is run from.
"""
import importlib.util
import os
import sys


def bind():
    """Make `sortie_kit` mean the package this file lies in, before anything imports it."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("sortie_kit", os.path.join(here, "__init__.py"),
                                                  submodule_search_locations=[here])
    module = importlib.util.module_from_spec(spec)
    sys.modules["sortie_kit"] = module
    spec.loader.exec_module(module)


if __name__ == "__main__":
    bind()
    from sortie_kit.cli import main
    raise SystemExit(main())
