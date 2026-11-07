# Vendored TOML reader

The parser source is the flat `../../../_tomli.py` module (Tomli 2.2.1, MIT,
Taneli Hukkinen; SPDX headers in the file, full text in `tomli/LICENSE`).
It is the single copy: Devin's closed payload ships `_tomli.py` because it
forbids `_measure` imports, and `_measure._vendor.tomli` re-exports it so
repo code keeps one lazy entry point. Python 3.11+ uses stdlib `tomllib`.

Source: https://pypi.org/project/tomli/2.2.1/
Archive: https://files.pythonhosted.org/packages/18/87/302344fed471e44a87289cf4967697d07e532f2421fdaf868a303cbae4ff/tomli-2.2.1.tar.gz
Archive SHA256: `cd45e1dc79c835ce60f7404ec8119f2eb06d38b1deba146f07ced3bbc44505ff`

`tomli/__init__.py` is a compatibility shim over `_tomli`; `LICENSE` and
`py.typed` are kept from the upstream package. It is not an extraction
provider or public API.
