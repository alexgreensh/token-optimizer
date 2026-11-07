# Vendored TOML reader

`tomli/` is the unmodified pure-Python source from Tomli 2.2.1, licensed under MIT (see `tomli/LICENSE`). It supplies TOML 1.0 parsing on Python 3.9 and 3.10 without a pip dependency. Python 3.11+ continues to use its standard-library `tomllib`.

Source: https://pypi.org/project/tomli/2.2.1/
Archive: https://files.pythonhosted.org/packages/18/87/302344fed471e44a87289cf4967697d07e532f2421fdaf868a303cbae4ff/tomli-2.2.1.tar.gz
Archive SHA256: `cd45e1dc79c835ce60f7404ec8119f2eb06d38b1deba146f07ced3bbc44505ff`

The source files and `py.typed` are copied from `src/tomli/`; `LICENSE` is copied from the archive root. Keep these files unchanged when updating. The package stays below `_measure` so the existing installed-source refusal boundary covers it too. It is not an extraction provider or public API.
