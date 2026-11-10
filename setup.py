from setuptools import setup, find_packages

# Runtime is stdlib-only (urllib/json); dynatrace_extension is supplied by the EEC.
# dt-extensions-sdk is listed so `dt-sdk build` and local imports resolve on a dev machine.
setup(
    name="netbox_network",
    version="0.1.0",
    description="NetBox -> Dynatrace network model (Semantic Dictionary)",
    packages=find_packages(exclude=["tests"]),
    python_requires=">=3.10",
    install_requires=["dt-extensions-sdk"],
)
